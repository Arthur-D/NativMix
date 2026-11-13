import os
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "packaging/update-nativmix.sh").read_text()


def run(args, cwd, ok=True, env=None):
    p = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    if ok and p.returncode:
        raise AssertionError(p.stdout)
    return p


def test_update_preserves_sources_and_build_artifacts():
    with tempfile.TemporaryDirectory(prefix="nativmix-update-test-") as temp:
        t = pathlib.Path(temp)
        origin = t / "origin"
        seed = t / "seed"
        bins = t / "bin"
        bins.mkdir()
        (bins / "flatpak").write_text("""#!/bin/bash
 printf '%s\\n' "$*" >> "$MOCK_LOG"
 if [[ $1 == run && $2 == --user ]]; then sleep 4; fi
 exit 0
 """)
        (bins / "flatpak").chmod(0o755)
        run(["git", "init", "--bare", str(origin)], t)
        run(["git", "init", "-b", "main", str(seed)], t)

        def git(p, *args, ok=True):
            return run(["git", *args], p, ok)

        def identity(p):
            git(p, "config", "user.name", "Updater Test")
            git(p, "config", "user.email", "test@example.invalid")
            git(p, "config", "commit.gpgsign", "false")

        identity(seed)
        (seed / "packaging").mkdir()
        (seed / "flatpak").mkdir()
        (seed / "packaging/update-nativmix.sh").write_text(SCRIPT)
        (seed / "flatpak/io.github.ArthurD.NativMix.yml").write_text("  - python3-deps.json\n        path: ..\n")
        (seed / ".gitignore").write_text(".flatpak-builder/\ndist/\nrepo/\n*.log\n")
        (seed / "tracked").write_text("base\n")
        (seed / "deleted").write_text("delete me\n")
        git(seed, "add", ".")
        git(seed, "commit", "-m", "base")
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "origin", "main")
        for case in ["clean", "dirty", "conflict", "staged-build", "commit-failure"]:
            p = t / case
            git(t, "clone", "-b", "main", str(origin), str(p))
            identity(p)
            for d in [".flatpak-builder", "dist", "repo", "builddir"]:
                (p / d).mkdir()
                (p / d / "artifact").write_text("preserve artifact")
            if case != "clean":
                (p / "tracked").write_text("local\n")
                (p / "deleted").unlink()
                for name in ["new source.py", "odd\nname.py", ":(glob)*.py"]:
                    (p / name).write_text("local source")
            if case == "staged-build":
                git(p, "add", "-f", "dist/artifact")
            if case == "commit-failure":
                hook = p / ".git/hooks/pre-commit"
                hook.write_text("#!/bin/sh\nexit 1\n")
                hook.chmod(0o755)
            # Advance the remote for every case, including a conflicting edit when requested.
            (seed / "remote-file").write_text(case)
            if case == "conflict":
                (seed / "tracked").write_text("remote\n")
            git(seed, "add", ".")
            git(seed, "commit", "-m", case)
            git(seed, "push", "origin", "main")
            env = os.environ.copy()
            env.update(
                PATH=str(bins) + ":" + env["PATH"], MOCK_LOG=str(t / (case + ".log")), XDG_STATE_HOME=str(t / "state")
            )
            result = run(["bash", "packaging/update-nativmix.sh"], p, False, env)
            log = pathlib.Path(env["MOCK_LOG"]).read_text()
            for d in [".flatpak-builder", "dist", "repo", "builddir"]:
                assert (p / d / "artifact").read_text() == "preserve artifact"
            if case in ["clean", "dirty"]:
                assert result.returncode == 0, result.stdout
                assert (p / "remote-file").read_text() == case
                assert "install --user" in log
                assert not git(p, "ls-files", "dist", "repo", ".flatpak-builder", "builddir").stdout
                if case == "dirty":
                    assert (p / "tracked").read_text() == "local\n"
                    assert not (p / "deleted").exists()
                    assert not git(p, "status", "--porcelain", "--untracked-files=all", "--", ".", ":!builddir").stdout
            else:
                assert result.returncode != 0, result.stdout
                assert "install --user" not in log
                assert "kill " not in log
                assert (p / "tracked").read_text() == "local\n"
                assert not (p / ".git/MERGE_HEAD").exists()
            print("PASS:", case)
