#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
package_dir="$(cd "$script_dir/.." && pwd -P)"
project_dir="$(cd "$script_dir/../../.." && pwd -P)"
requested_output="${CCSHIFT_APP_OUTPUT:-$package_dir/dist/ccshift.app}"
if [[ "$requested_output" != /* || "$(basename "$requested_output")" != "ccshift.app" ]]; then
  printf 'CCSHIFT_APP_OUTPUT must be an absolute path ending in ccshift.app.\n' >&2
  exit 1
fi
output_parent="$(dirname "$requested_output")"
if [[ -L "$output_parent" ]]; then
  printf 'Refusing to build through a symlinked output directory: %s\n' "$output_parent" >&2
  exit 1
fi
mkdir -p "$output_parent"
dist_dir="$(cd "$output_parent" && pwd -P)"
app_path="$dist_dir/ccshift.app"
build_path=""
staging=""
bundle_id="com.namsequence.ccshift.menubar"
app_version="${CCSHIFT_APP_VERSION:-$(sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' "$project_dir/pyproject.toml" | head -n 1)}"
if [[ ! "$app_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  printf 'CCSHIFT_APP_VERSION must use MAJOR.MINOR.PATCH.\n' >&2
  exit 1
fi
build_path="$(mktemp -d "${TMPDIR:-/tmp}/ccshift-menubar-build.XXXXXX")"

cleanup() {
  if [[ -n "$staging" && -d "$staging" ]]; then rm -rf "$staging"; fi
  if [[ -n "$build_path" && -d "$build_path" ]]; then rm -rf "$build_path"; fi
}
trap cleanup EXIT

if [[ -e "$app_path" ]]; then
  if [[ -L "$app_path" || ! -d "$app_path" ]]; then
    printf 'Refusing to replace an unsafe app path: %s\n' "$app_path" >&2
    exit 1
  fi
  existing_id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app_path/Contents/Info.plist" 2>/dev/null || true)
  if [[ "$existing_id" != "$bundle_id" ]]; then
    printf 'Refusing to replace an app with an unexpected bundle identifier: %s\n' "$app_path" >&2
    exit 1
  fi
fi

trap cleanup EXIT
swift build --configuration release --product CcshiftMenuBar \
  --package-path "$package_dir" --build-path "$build_path"

staging=$(mktemp -d "$dist_dir/.ccshift.app.XXXXXX")
mkdir -p "$staging/Contents/MacOS" "$staging/Contents/Resources"
cp "$build_path/release/CcshiftMenuBar" "$staging/Contents/MacOS/ccshift"
chmod 755 "$staging/Contents/MacOS/ccshift"

# The Icon Composer icon compiles to Assets.car (Liquid Glass on macOS 26) plus
# an AppIcon.icns fallback for macOS 14 and 15. actool ships with Xcode only;
# without it the app is built without an icon.
icon_plist_keys=""
if xcrun --find actool >/dev/null 2>&1; then
  xcrun actool "$package_dir/Resources/AppIcon.icon" \
    --compile "$staging/Contents/Resources" \
    --platform macosx --target-device mac --minimum-deployment-target 14.0 \
    --app-icon AppIcon --output-partial-info-plist "$build_path/icon-info.plist" \
    >/dev/null
  icon_plist_keys="<key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleIconName</key><string>AppIcon</string>"
else
  printf 'actool not found (install Xcode); building without the app icon.\n' >&2
fi

cat > "$staging/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleExecutable</key><string>ccshift</string>
  ${icon_plist_keys}
  <key>CFBundleIdentifier</key><string>${bundle_id}</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>ccshift</string>
  <key>CFBundleDisplayName</key><string>ccshift</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${app_version}</string>
  <key>CFBundleVersion</key><string>${app_version}</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

plutil -lint "$staging/Contents/Info.plist"
codesign --force --deep --sign - --timestamp=none "$staging"

if [[ -e "$app_path" ]]; then
  rm -rf "$app_path"
fi
mv "$staging" "$app_path"
staging=""
rm -rf "$build_path"
trap - EXIT
printf 'Built menu bar app: %s\n' "$app_path"
