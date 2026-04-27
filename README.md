# Drop-in AI Toolkit

A powerful, modular AI toolkit designed to be dropped into any project. It auto-detects your project structure and uses local LLMs (via Ollama) to scaffold code, generate tests, perform code reviews, and automatically apply fixes.

## Features

- **Project Detection (`detect.py`)**: Automatically detects the project stack, languages, and directory structure.
- **Rules Engine (`rules.py`)**: Generates architecture and layer rules based on your `ARCHITECTURE.md` and project structure.
- **Development Scaffolding (`develop.py`)**: Translates your architecture document into actual implementation code with the proper patterns.
- **Test Generation (`testgen.py`)**: Analyzes existing code and generates comprehensive unit and integration test suites.
- **Code Review (`review.py`)**: Performs a layer-aware code review against generated rules, finding bugs and architecture violations.
- **Auto-Fixer (`fix.py`)**: Applies fixes recommended by the code review tool automatically.

## Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com/) running locally or on a remote host

## Usage

The single entry point to the toolkit is `drop.py`. Drop this toolkit into your project or invoke it directly.

```bash
# Detect project structure and show plan
python drop.py

# Scaffold code from an architecture doc
python drop.py develop
python drop.py develop --apply

# Generate test suites
python drop.py test
python drop.py test --apply

# Review code
python drop.py review

# Apply review fixes
python drop.py fix --apply

# Full pipeline
python drop.py all --apply
```

### Model Configuration

By default, the toolkit expects Ollama to be running on `http://localhost:11434`. You can point it to a different host:

```bash
python drop.py --url http://remote_host:11434
```

The engine intelligently routes tasks to different models depending on the complexity of the task (e.g. reasoning, coding, or quick classification).
