# Test Version Support

## Overview

The installer now supports a test mode that allows QA testers to receive pre-release/test versions without requiring a separate installer build. This is controlled by a `.env` file placed next to the installer executable.

## How It Works

### For QA Testers

1. **Copy the test configuration file**:
   - Locate `.env.tester` in the project repository
   - Copy it to the same directory as your installer executable

2. **Rename the file**:
   - Rename `.env.tester` to `.env` (just `.env`, no extension)

3. **Run the installer**:
   - Launch the installer normally
   - You should see `[TEST VERSIONS]` in the window title
   - The installer will now fetch and install test versions

4. **To disable test mode**:
   - Simply delete or rename the `.env` file
   - Restart the installer

### Visual Indicators

When test mode is active:
- Window title shows: `FryNetworks Miners and Nodes Installer vX.X.X [TEST VERSIONS]`
- Debug log contains: `TEST MODE ENABLED: Using test-windows/test-linux platforms for version checks`

### API Behavior

**Normal Mode**:
- Queries: `GET /versions/{miner_code}?platform=windows` or `platform=linux`
- Returns production versions

**Test Mode**:
- Queries: `GET /versions/{miner_code}?platform=test-windows` or `platform=test-linux`
- Returns test versions for QA validation

## Implementation Details

### Changes Made

1. **tools/external_api.py**:
   - Added `use_test` parameter to `get_required_version()`
   - When `use_test=True`, queries `test-windows` or `test-linux` platforms

2. **gui/installer_window.py**:
   - Added `_load_test_mode_config()` method to read `.env` file
   - Added `_use_test_versions` flag initialized in `__init__`
   - Updated window title to show `[TEST VERSIONS]` suffix when active
   - Updated all `get_required_version()` calls to pass `use_test` parameter

3. **Configuration Files**:
   - `.env.tester`: Template file for QA testers
   - Contains `ENABLE_TEST_VERSIONS=true` setting

### .env File Format

```env
# Enable test version mode
ENABLE_TEST_VERSIONS=true

# Optional: Security key (not implemented yet)
# TEST_MODE_KEY=internal_qa_2024
```

Supported values for `ENABLE_TEST_VERSIONS`:
- `true`, `1`, `yes`, `on` → Test mode enabled
- `false`, `0`, `no`, `off` → Test mode disabled
- Missing file or unrecognized value → Test mode disabled (safe default)

## Security Considerations

### Safe Implementation

1. **No security bypass**: Test mode only affects version queries, not authentication
2. **Easy to detect**: Window title clearly shows when test mode is active
3. **Logging**: All test mode activations are logged to debug files
4. **Reversible**: Delete/rename `.env` file to return to normal mode
5. **No code changes**: Same binary works for both production and testing

### Best Practices

- **Distribution**: Only share `.env` file with authorized QA testers
- **Visibility**: Test mode is clearly visible in the UI (window title)
- **Auditability**: All test mode usage is logged with timestamps
- **Optional security**: `TEST_MODE_KEY` field reserved for future validation

## Benefits

1. **Single Binary**: Same installer.exe for production and QA testing
2. **Easy QA Workflow**: Testers just copy a file to switch modes
3. **No Rebuild Required**: Update test versions without recompiling installer
4. **Safe Rollback**: Delete `.env` file to return to production versions
5. **Standard Practice**: Industry-standard .env pattern for configuration

## Future Enhancements

Potential improvements:
- Implement `TEST_MODE_KEY` validation for security
- Add visual warning banner in the UI when test mode is active
- Add expiration date to test mode configurations
- Log test mode installations to separate analytics endpoint
