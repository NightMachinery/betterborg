# Task Completion Checklist for BetterBorg

## After Making Code Changes

### No Formal Testing/Linting
- **Important**: This project does not have formal testing, linting, or formatting commands
- No `npm run test`, `pytest`, `flake8`, or similar commands are configured
- Development is done through manual testing via Telegram interface

### Manual Verification Steps
1. **Plugin Hot Reload**: If modifying plugins, the system will automatically reload them due to file watching
2. **Manual Testing**: Test functionality through the Telegram bot interface
3. **Check Logs**: Monitor console output for errors or exceptions
4. **Admin Commands**: Use admin-only commands to verify privileged functionality works

### Bot Command Registration
- If adding new slash commands, ensure `BOT_COMMANDS` list is updated in the plugin
- Verify commands appear in Telegram's command menu
- Update plugin's `/help` command documentation if needed

### Environment-Specific Testing
- Test different plugin configurations (`borg_plugin_path`)
- Verify functionality across different session types (stt, llm_chat, etc.)
- Test both standalone and server modes if applicable

### Before Committing
- Ensure no sensitive information (API keys, tokens) is committed
- Check that new dependencies are added to `requirements.txt` if needed
- Verify plugin hot reload works correctly with changes

## What NOT to do
- Don't look for pytest, unittest, or other testing frameworks (they don't exist)
- Don't run linting commands like flake8, black, or mypy (not configured)
- Don't expect CI/CD pipelines (this is a personal/small project)

## Development Workflow
The typical workflow is:
1. Make code changes
2. Plugin auto-reloads (if file watching is active)
3. Test through Telegram interface
4. Monitor console for errors
5. Iterate as needed