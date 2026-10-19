# Contributing Guide

Thank you for your interest in this project! Contributions of all kinds are welcome.

## 🐛 Reporting Bugs

1. Search the [existing issues](https://github.com/ZhuLinsen/daily_stock_analysis/issues) to make sure the problem has not already been reported.
2. Create a new issue using the Bug Report template.
3. Provide detailed reproduction steps and information about your environment.

## 💡 Suggesting Features

1. Search the existing issues to make sure the feature has not already been suggested.
2. Create a new issue using the Feature Request template.
3. Describe your use case and the expected functionality in detail.

## 🔧 Contributing Code

### Development Environment

```bash
# Clone the repository
git clone https://github.com/ZhuLinsen/daily_stock_analysis.git
cd daily_stock_analysis

# Create a virtual environment
python -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
```

### Contribution Workflow

1. Fork this repository.
2. Create a feature branch:

   ```bash
   git checkout -b feature/your-feature
   ```

3. Commit your changes:

   ```bash
   git commit -m "feat: add some feature"
   ```

4. Push the branch:

   ```bash
   git push origin feature/your-feature
   ```

5. Open a Pull Request.

### Commit Convention

Follow the [Conventional Commits specification](https://www.conventionalcommits.org/):

```text
feat: add a new feature
fix: fix a bug
docs: update documentation
style: update code formatting without changing functionality
refactor: refactor existing code
perf: improve performance
test: add or update tests
chore: update build tools or project tooling
```

Examples:

```text
feat: add DingTalk bot support
fix: correct retry logic for 429 rate-limit responses
docs: update deployment instructions in the README
```

### Code Standards

- Python code must follow PEP 8.
- Functions and classes must include docstrings.
- Add comments for important or complex logic.
- Update the relevant documentation when adding new features.

### Automated CI Checks

After a Pull Request is submitted, CI automatically runs the checks described below.

When a change only affects regular files under `docs/**`, non-governance Markdown files, or `LICENSE`, CI retains lightweight change detection, governance validation, and gate-summary checks. However, it skips the three backend test shards, Docker checks, Web checks, and desktop packaging.

Contract documentation, static API specifications, and Markdown fixtures that are read directly by offline tests still trigger backend testing.

Governance assets such as `AGENTS.md`, `.github/instructions/**`, and `.claude/skills/**` are still validated by `ai-governance`. Configuration files, dependencies, workflows, and other text files that can affect runtime behavior still trigger their corresponding full checks.

| Check | Description | Required |
|---|---|:---:|
| `backend-gate` | Runs `scripts/ci_gate.sh`, including `py_compile`, critical `flake8` checks, local core scripts, and offline `pytest` tests | ✅ |
| `docker-build` | Builds the Docker image and performs smoke tests for key module imports | ✅ |
| `web-gate` | Runs `npm run lint` and `npm run build` when frontend changes are detected | ✅ When triggered |
| `pr-review` | Automatic Pull Request triggering is paused. Maintainers can run it manually through `workflow_dispatch` using the Pull Request number. It reads Pull Request metadata and diffs through the GitHub API without checking out or executing code from forked Pull Requests. | ❌ Supporting check |
| `network-smoke` | Runs `pytest -m network` and `scripts/test.sh quick` on a schedule or through manual execution. This check is non-blocking. | ❌ Observational check |

**Running checks locally:**

```bash
# Backend gate, recommended
pip install -r requirements.txt
pip install flake8 pytest
./scripts/ci_gate.sh

# Frontend gate, when modifying apps/dsa-web
cd apps/dsa-web
npm ci
npm run lint
npm run build
```

## 📋 Priority Contribution Areas

See the ../README.md for an overview of the project's current core capabilities and main entry points.

- 🔔 New notification channels, such as DingTalk, Feishu, and Telegram
- 🤖 Support for additional AI models, such as GPT-4 and Claude
- 📊 Integration with additional data sources
- 🐛 Bug fixes and performance improvements
- 📖 Documentation improvements and translations

## ❓ Questions and Support

If you have any questions, you are welcome to:

- Create an issue for discussion.
- Review existing issues and discussions.

Thank you again for your contribution! 🎉
