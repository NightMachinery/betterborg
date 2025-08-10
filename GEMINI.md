------------------------------------------------------------------------

-   DRY.

    -   Find common patterns in the code that can refactored into shared code.

-   Use dependency injection to improve code flexibility - let components receive their dependencies from outside instead of hardcoding them. For example, pass configurations as arguments or inject service instances through constructors. However, never inconvenience the user. The dependencies must always be optional to provide.

-   Do NOT add comments about what you have changed, e.g., `newly added`. The user uses version control software to manually review the changes.

------------------------------------------------------------------------

# Functions

-   Have any non-obvious function arguments be keyword arguments. Have at most two positional arguments. Use `(pos_arg1, ..., *, kwarg,)` to enforce keyword argument usage.

# Conditionals

For enum-like conditionals, use explicit matching conditions, and raise an exception on `else` (when it signifies an unknown value).

------------------------------------------------------------------------

# Project Specific

1. Start your session by reading the files: uniborg/*.py stt_plugins/*.py llm_chat_plugins/*.py

