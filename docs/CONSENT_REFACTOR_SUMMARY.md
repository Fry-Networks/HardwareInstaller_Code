# Bandwidth Sharing Tools Consent Refactor (v3.9.0)

## Overview
Major architectural change separating installer concerns from miner GUI activation logic. The installer is now "dumb" - it stages all bandwidth sharing SDK files but doesn't handle any consent dialogs or activation. All consent and activation is handled by the miner GUI.

## New fVPN Reward Multiplier System
- **Base reward**: 0.25x multiplier for fVPN token earnings
- **Each bandwidth sharing tool adds**: +0.25x multiplier
- **Maximum multiplier**: 1.00x (with all 3 tools enabled)
- **Tools**: Honeygain, Mysterium VPN, Bright Data Web Indexing

## Changes Made

### 1. Installer GUI (`gui/installer_window.py`)
**Removed:**
- `_create_partner_consent_section()` - checkbox UI for Honeygain/Mysterium/Bright
- `_show_partner_disclaimer()` - Honeygain and Mysterium consent dialogs
- `_show_bright_consent_dialog()` - Bright consent dialog with custom UI
- `_handle_bright_consent_link()` - Modal popups for "approved uses" and "never used for"
- `_invoke_bright_api()` - Bright SDK consent API calls via pythonnet
- `_on_bright_toggled()` - Bright checkbox toggle handler
- `_handle_partner_toggle()` - Generic partner toggle handler
- All references to `honeygain_checkbox`, `mysterium_checkbox`, `bright_checkbox`

**Added:**
- `_create_bm_rewards_info()` - Simple informational message explaining the new fVPN reward multiplier system
- Updated all partner-related references to use `bm_rewards_group` instead of `partner_group`

**Modified:**
- Review summary no longer displays partner SDK status (no checkboxes to check)
- All opt-in values always set to False in installation options
- Removed validation warnings for missing credentials (silently skip instead)
- `_stage_partner_sdks` still sent to service_manager to stage available SDKs

### 2. Service Manager (`core/service_manager.py`)
**Function: `_prepare_partner_integrations()`**

**Old behavior:**
- If user opted in via checkbox → stage SDK and activate
- If credentials missing → raise RuntimeError and fail installation
- Respect explicit opt-ins from installer UI

**New behavior:**
- Installer always stages ALL available SDKs for BM installs
- All opt-ins default to False (stored in sdk_config.enc)
- If credentials missing → silently skip that SDK (no error)
- GUI handles consent dialogs and activation (not installer)

**Code changes:**
```python
# OLD:
hg_needed = bool(opts.get("honeygain_opt_in")) or stage_hg
if not api_key:
    raise RuntimeError("Credentials missing...")

# NEW:
hg_needed = stage_hg  # Only check staging flag, ignore opt-in
if api_key:
    actions.extend(_configure_honeygain_assets(...))
# else: Silently skip if credentials not available
```

### 3. SDK Config Creation
**File: `sdk_config.enc`**

Already correctly implemented in `_build_sdk_approval_payload()`:
- Defaults all approvals to False
- Respects opt-in values from options (which are all False now)
- GUI will update this file when user activates tools

## Installation Flow (After Changes)

### Installer Behavior:
1. User enters miner key (BM code detected)
2. No consent dialogs shown
3. Simple info message explains fVPN reward system
4. Installer stages ALL available SDK files:
   - Honeygain SDK + config (if credentials embedded)
   - Mysterium SDK + config (always)
   - Bright SDK + config (if credentials embedded and Windows)
5. Creates `sdk_config.enc` with ALL approvals set to False
6. Launches miner GUI

### Miner GUI Behavior (Future Implementation):
1. Detects staged SDKs from installer
2. Shows dedicated tabs/sections for each bandwidth sharing tool
3. User toggles on a tool → GUI shows consent dialog
4. User accepts → GUI updates `sdk_config.enc` and activates SDK
5. fVPN reward multiplier updates: 0.25x → 0.50x → 0.75x → 1.00x

## Files Modified
- `gui/installer_window.py` (~240 lines removed, ~20 lines added)
- `core/service_manager.py` (~40 lines modified)
- `docs/README.md` (updated "Bandwidth Sharing Tools" section)

## Testing Checklist
- [ ] BM install completes without consent dialogs
- [ ] All 3 SDKs staged to correct directories (if credentials available)
- [ ] `sdk_config.enc` created with all approvals = false
- [ ] Info message displays correct fVPN reward multiplier information
- [ ] No errors when credentials missing (silently skips)
- [ ] Installer launches GUI successfully after install

## Breaking Changes
None for end users. This is purely an architectural change - the installer is now simpler and the GUI will handle all activation logic in future releases.

## Benefits
1. **Cleaner separation of concerns**: Installer just copies files, GUI handles activation
2. **Better user experience**: Consent dialogs in context where they're actually used
3. **Simpler installer**: No complex consent flow, pythonnet calls, or checkbox state management
4. **Flexibility**: Users can activate/deactivate tools anytime from GUI (not just during install)
5. **Clear reward model**: Users understand they get +0.25x per tool, up to 1.00x total
