# Agent Instructions

## Workflow

- Work on `dev` branch
- Make atomic commits
- Do not merge to `main` unless the user explicitly asks for it
- When a change is merged to `main`, determine the next version bump
    automatically from the code changes, create the new tag automatically,
    and push all resulting commits and tags

---

## Commit Message Format

Follow conventional commit format. First line must be less than 78 characters.

### Format

```text
<type>(<scope>): <icon> <short description>
```

### Types

- `feat`: ✨ A new feature
- `fix`: 🐛 A bug fix
- `docs`: 📝 Documentation updates
- `style`: 💄 Formatting, missing semi-colons
- `refactor`: ♻️ Code change that neither fixes a bug nor adds a feature
- `test`: ✅ Adding or correcting tests
- `chore`: 🔧 Build process or auxiliary tools
- `ci`: 👷 CI configuration files and scripts
- `build`: 🏗️ Build system changes
- `revert`: ⏪ Reverting changes
- `wip`: 🚧 Work in progress

### Rules

- Long description should explain why the change was made
- Reference related issues (e.g., `Fixes #123`)
- If breaking change, include `BREAKING CHANGE: <description>`

---

## Python Coding Conventions (applyTo: **/*.py)

### General

- Write clear and concise comments for each function
- Use descriptive names with type hints
- Break down complex functions into smaller ones
- Prioritize readability and clarity

### Documentation

- Use numpy-style docstrings (PEP 257)
- Include docstrings immediately after `def` or `class`
- Docstring format:

  ```python
  def function(param: type) -> return_type:
      """Short summary.

      Longer description if needed.

      Parameters:
          param (type): Description

      Returns:
          return_type: Description
      """
  ```

### Style

- Follow **PEP 8** style guide
- Use 4 spaces for indentation
- Max line length: 79 characters
- Group imports: stdlib, third-party, local (separated by blank lines)

### Testing

- Include test cases for critical paths
- Account for edge cases (empty inputs, invalid types)

---

## Markdown Conventions (applyTo: **/*.md)

Follow all markdownlint (MDxxx) rules:

- Use ATX-style headers (`#`, `##`, etc.)
- Include a blank line after headers
- Use fenced code blocks with language identifiers
- Keep lines under 80 characters
- Use consistent indentation (2 or 4 spaces)
- No trailing whitespace
- Include alt text for images
- Use inline links, keep URLs on separate lines if long
- Use ordered lists for sequential steps, unordered for non-sequential
- Use consistent quote styles (double quotes)
- Include proper spacing between block elements
