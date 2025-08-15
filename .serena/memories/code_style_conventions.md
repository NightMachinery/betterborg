# BetterBorg Code Style and Conventions

## Code Style Guidelines from CLAUDE.md

### DRY Principle
- Find common patterns that can be refactored into shared code
- Avoid code duplication

### Dependency Injection
- Use dependency injection to improve code flexibility
- Let components receive dependencies from outside instead of hardcoding them
- Pass configurations as arguments or inject service instances through constructors
- **Important**: Dependencies must always be optional to provide (never inconvenience the user)

### Comments Policy
- **DO NOT** add comments about what you have changed (e.g., `newly added`)
- User relies on version control software to manually review changes
- Generally minimal commenting approach

### Function Design
- Have any non-obvious function arguments be keyword arguments
- Have at most two positional arguments
- Use `(pos_arg1, ..., *, kwarg,)` to enforce keyword argument usage

### Conditionals
- For enum-like conditionals, use explicit matching conditions
- Raise an exception on `else` when it signifies an unknown value

## Observed Code Patterns

### Plugin Structure
```python
# Standard plugin pattern
from telethon import events

@borg.on(events.NewMessage(pattern='pattern'))
async def handler(event):
    await event.reply('response')
```

### Variable Injection
- Plugins automatically get `borg`, `logger`, `storage` variables
- Use these injected variables rather than imports

### Async/Await Usage
- Heavy use of asyncio and async/await patterns
- Event handlers are async functions

### Admin System
- Use `util.isAdmin()` to check admin permissions
- Admin users defined in `uniborg/util.py`

### Error Handling
- Use try/except blocks for external API calls
- Print errors and tracebacks for debugging

### Bot Commands Registration
- When adding slash commands, update `BOT_COMMANDS` list in the plugin
- Use dynamic handler registration pattern for bot username support