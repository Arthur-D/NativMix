#!/bin/bash

# Configuration - Modular variables to avoid hardcoding
APP_VERSION="1.0.3"
SPEC_FILE="packaging/suse/nativmix.spec"
RPM_ROOT="$HOME/rpmbuild"
SOURCES_DIR="${RPM_ROOT}/SOURCES"
SPECS_DIR="${RPM_ROOT}/SPECS"
RPMS_DIR="${RPM_ROOT}/RPMS"
MIDO_URL="https://files.pythonhosted.org/packages/source/m/mido/mido-1.3.2.tar.gz"

echo "Starting automated RPM build for NativMix v${APP_VERSION}..."

# 1. Create the required rpmbuild directory structure
mkdir -p "${SOURCES_DIR}" "${SPECS_DIR}" "${RPM_ROOT}/BUILD" "${RPMS_DIR}" "${RPM_ROOT}/SRPMS"

# 2. Export the current directory into a tarball (including uncommitted changes)
echo "--> Generating source tarball from current directory..."
# We use tar instead of git archive to ensure uncommitted changes (like version bumps) are included.
tar --exclude='.git' --exclude='rpmbuild' --exclude='pkg' --exclude='dist' --exclude='build' \
    -czf "${SOURCES_DIR}/nativmix_${APP_VERSION}.orig.tar.gz" \
    --transform "s|^|NativMix-${APP_VERSION}/|" .

# 3. Fetch external dependencies (mido)
echo "--> Fetching external dependency: mido..."
curl -L --silent --show-error "${MIDO_URL}" -o "${SOURCES_DIR}/mido-1.3.2.tar.gz"

# 4. Copy auxiliary source files (udev rules)
echo "--> Copying auxiliary source files..."
# Rule 2: Ensure correct path for udev rules
UDEV_RULE="data/udev/99-nativmix-arduino.rules"
if [ -f "$UDEV_RULE" ]; then
    cp "$UDEV_RULE" "${SOURCES_DIR}/"
else
    echo "Warning: $UDEV_RULE not found. Continuing anyway..."
fi

# 5. Copy the spec file to the build tree
echo "--> Deploying spec file..."
cp "${SPEC_FILE}" "${SPECS_DIR}/"

# 5. Execute the RPM build process
echo "--> Building the RPM package..."
rpmbuild -ba "${SPECS_DIR}/nativmix.spec"

# 6. Check if the build was successful and locate the output file
if [ $? -eq 0 ]; then
    echo "========================================="
    echo "Success! The RPM package has been built."
    echo "You can find it here:"
    ls -lh "${RPMS_DIR}/noarch/"nativmix-${APP_VERSION}-*.rpm
    echo "========================================="
else
    echo "Error: RPM build failed. Please check the output above."
    exit 1
fi

if [ $? -eq 0 ]; then
    echo "--> Build successful!"
    read -p "Do you want to install the new version now? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        sudo zypper install --allow-unsigned-rpm ~/rpmbuild/RPMS/noarch/nativmix-*.noarch.rpm
    fi
else
    echo "Error: RPM build failed."
    exit 1
fi