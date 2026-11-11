#!/usr/bin/env bash
# Update and launch the NativMix fork Flatpak. Run directly from fish or Bash.
# Local source changes are committed before merging origin/main; nothing is pushed.
set -euo pipefail

script_path=$(readlink -f -- "${BASH_SOURCE[0]}")
repo=$(cd -- "$(dirname -- "$script_path")/.." && pwd -P)
app="io.github.ArthurD.NativMix"
manifest="flatpak/$app.yml"

if [[ ${1:-} == --help ]]; then
    printf 'Usage: %s [--help]\nSave local changes, merge origin/main, build/install the user Flatpak, and launch NativMix.\n' "$0"
    exit 0
fi
[[ $# == 0 ]] || { printf 'Unknown argument: %s\n' "$1" >&2; exit 2; }

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
trap 'printf "Update aborted (line %s). See the error above.\n" "$LINENO" >&2' ERR

for program in git flock flatpak nohup python3; do
    command -v "$program" >/dev/null || die "Missing required command: $program"
done
[[ -e "$repo/.git" ]] || die "Cannot find Git checkout: $repo"

# Prevent simultaneous runs of this script.
lock_path=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir)
exec 9>"$lock_path/nativmix-update.lock"
flock -n 9 || die "Another NativMix update is already running."

cd "$repo"
# Generated Flatpak output must not enter automatic source commits.
source_paths=(. ':!repo' ':!dist' ':!builddir' ':!build-dir' ':!.flatpak-builder' ':!build-flatpak' ':!.nativmix-build.*.yml')
flatpak info org.flatpak.Builder >/dev/null ||
    die "Missing org.flatpak.Builder. Install it with: flatpak install flathub org.flatpak.Builder"
[[ "$(git symbolic-ref --quiet --short HEAD)" == main ]] ||
    die "The checkout must be on main."
for state in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
    [[ ! -e "$(git rev-parse --git-path "$state")" ]] ||
        die "A Git operation is in progress ($state). Complete or abort it first."
done

printf 'Updating NativMix from origin/main ...\n'
git fetch origin main
# Save tracked changes and new, non-ignored files as a local commit.
if [[ -n "$(git status --porcelain --untracked-files=all -- "${source_paths[@]}")" ]]; then
    printf 'Saving local changes and new files in a commit ...\n'
    git add --all -- "${source_paths[@]}"
    if ! git diff --cached --quiet; then
        git commit -m "Save local changes before NativMix update" ||
            die "Could not commit local changes. Check the Git error above; the app has not been restarted."
    fi
    [[ -z "$(git status --porcelain --untracked-files=all -- "${source_paths[@]}")" ]] ||
        die "Local changes remain (possibly in a submodule). Check them manually; the app has not been restarted."
fi
# FETCH_HEAD is the exact branch just fetched, regardless of refspec configuration.
# Fast-forward when possible; preserve local commits with a merge otherwise.
if ! git -c merge.autoStash=false merge --no-edit --ff FETCH_HEAD; then
    if [[ -f "$(git rev-parse --git-path MERGE_HEAD)" ]]; then
        git merge --abort ||
            die "Could not abort the merge. Check the checkout manually; the app has not been restarted."
    fi
    die "Could not merge origin/main automatically. Resolve this in $repo; the app has not been restarted."
fi
[[ -f "$manifest" ]] || die "Missing Flatpak manifest: $manifest"
printf 'Building Flatpak (the first build may take a long time) ...\n'
# Sandboxed Builder requires directory sources to stay inside the manifest's
# directory. Rebase a temporary manifest to the checkout root, leaving the
# tracked manifest untouched. Exclude build output from the directory source.
build_manifest=$(mktemp "$repo/.nativmix-build.XXXXXXXX.yml")
trap 'rm -f -- "$build_manifest"' EXIT
python3 - "$manifest" "$build_manifest" <<'PYMANIFEST'
import pathlib
import re
import sys

source, destination = map(pathlib.Path, sys.argv[1:])
text = source.read_text()
text, dependencies = re.subn(
    r"(?m)^(  - )python3-deps\.json\s*$",
    r"\1flatpak/python3-deps.json", text,
)
text, sources = re.subn(
    r"(?m)^        path: \.\.\s*$",
    "        path: .\n        skip:\n"
    "          - .git\n          - .flatpak-builder\n"
    "          - repo\n          - dist\n          - builddir\n"
    "          - build-dir\n          - build-flatpak\n          - .venv\n"
    "          - .nativmix-build.*.yml\n",
    text,
)
if dependencies != 1 or sources != 1:
    raise SystemExit("Manifest layout changed; cannot safely prepare the local build.")
destination.write_text(text)
PYMANIFEST
flatpak run --command=flathub-build org.flatpak.Builder "$build_manifest"
rm -f -- "$build_manifest"
trap - EXIT
mkdir -p dist
# build-bundle refuses to overwrite an existing bundle.
bundle_dir=$(mktemp -d "$repo/dist/update.XXXXXXXX")
printf 'Creating installable Flatpak bundle (this can take several minutes without output) ...\n'
flatpak build-bundle repo "$bundle_dir/$app.flatpak" "$app"
# Leave the running app alone until the build and bundle have succeeded.
printf 'Stopping NativMix ...\n'
running_apps=$(flatpak ps --columns=application)
if [[ $'\n'"$running_apps"$'\n' == *$'\n'"$app"$'\n'* ]]; then
    flatpak kill "$app"
fi
printf 'Installing updated Flatpak ...\n'
flatpak install --user --reinstall --assumeyes --noninteractive "$bundle_dir/$app.flatpak"
rm -f -- "$bundle_dir/$app.flatpak"
rmdir -- "$bundle_dir"
printf 'Starting NativMix ...\n'
log_dir="${XDG_STATE_HOME:-$HOME/.local/state}/nativmix"
mkdir -p "$log_dir"
# The launched app must not inherit the updater lock.
nohup flatpak run --user "$app" > "$log_dir/gui.log" 2>&1 9>&- < /dev/null &
gui_pid=$!
sleep 3
kill -0 "$gui_pid" 2>/dev/null ||
    die "The app exited during startup. See $log_dir/gui.log"
printf 'NativMix updated and started. Commit: %s\n' "$(git rev-parse --short HEAD)"
printf 'Log: %s\n' "$log_dir/gui.log"
