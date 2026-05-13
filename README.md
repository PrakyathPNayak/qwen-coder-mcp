# Qwen Coder MCP: Your Local AI Coding Assistant

Welcome to **Qwen Coder MCP**! This tool is a personal, AI-powered coding assistant designed to run entirely locally on your workstation (optimized for a single RTX 4090 GPU). It connects a powerful AI model to your favorite coding environment, like VS Code, Claude Desktop, or Continue. 

Beyond just answering questions, this tool features an "autonomous loop." When activated, it will continuously review your code, find bugs, propose fixes, double-check its own work, and save the accepted changes directly to your project.

> **Designed for Local Use:** This is a tool for your personal computer. It is entirely private and is not meant to be hosted on a public web server. It has the ability to read your files, execute shell commands, and browse the web, but it operates with safety guardrails built around a single-user workflow.

## Quick Start (Local RTX 4090 Setup)

Getting started is straightforward. Run these commands in your terminal:

```bash
./scripts/serve_qwen.sh        # Starts the local AI model
./scripts/wait_ready.sh        # Waits until the AI is fully loaded and ready
cp .env.example .env           # Creates your default configuration file
./scripts/run_loop.sh          # Starts the autonomous coding loop in the background
```

## What Can It Do?

When you connect Qwen Coder to your code editor, it provides a suite of practical tools:

*   **Chat & Explain:** Have free-flowing conversations about your codebase or ask the AI to explain complex snippets in plain English.
*   **Write & Complete Code:** Get intelligent code completions or ask the AI to generate unit tests for your existing work.
*   **Review & Refactor:** Have the AI scan for bugs, suggest improvements, and propose unified fixes.
*   **Devil's Advocate:** The AI can critique its own proposed changes to ensure high-quality code.
*   **Web Research:** The AI can browse the web via DuckDuckGo or Perplexity to find up-to-date documentation or solutions.

## The Terminal User Interface (TUI)

If you prefer staying in the terminal, we have a built-in Textual UI. 

To install and run it:
```
pip install -e '.[tui]'
qwen-coder-tui
```

In the TUI, anything you type is treated as a normal chat message. If you want to use a specific tool, just type `/` followed by the command. You can press TAB for auto-completion.

### Useful Commands

**Working with your files:**
*   `/read <path>`: Read a file or a specific set of lines.
*   `/grep <pattern>`: Search your entire project for a specific word or phrase.
*   `/diff`: See what changes have been made to your files.
*   `/apply`: Apply the code changes the AI just suggested.

**Browsing the web:**
*   `/search <query>`: Do a quick web search.
*   `/perplexity_ask <question>`: Get detailed, well-researched answers (requires a Perplexity API key).

**Running the Agent:**
*   `/agent <task>`: Give the AI a specific task to complete automatically.
*   `/loop start` and `/loop stop`: Turn the continuous self-improvement background loop on or off.
*   `/run <command>`: Execute a shell command. 

## Connecting to Your Code Editor

To use this with an editor like VS Code or Claude Desktop, install the package and point your editor to the server binary:

```bash
pip install -e .
qwen-coder-mcp
```

## Configuration

Your settings live in the `.env` file. You can leave most of these at their defaults, but here are the most useful ones to know:

*   `QWEN_MAX_TOKENS`: The maximum length of the AI's response. The default is high to give the AI plenty of room to "think" before answering.
*   `QWEN_AUTO_CONTINUE`: Set to `1` by default. If the AI is writing a massive block of code and runs out of room, it will automatically continue exactly where it left off instead of cutting off mid-sentence.
*   `LOOP_INTERVAL_SECONDS`: How long the autonomous loop pauses between checking your code (default is 45 seconds).
*   `PERPLEXITY_API_KEY`: Add your API key here if you want to use the advanced web research tools.

## Keeping You Safe

Because this tool can edit your files and run shell commands, it has built-in safety nets:

*   **You Are in Control:** By default, anytime the AI wants to do something destructive like write to a file or run a shell command, a prompt will pop up asking for your permission. You have 30 seconds to approve it, or the action is blocked.
*   **Sandboxing:** The AI is strictly locked to the project folder you opened it in. It cannot access files elsewhere on your computer.
*   **Revert on Failure:** When the autonomous loop tries to fix code, it tests the changes first. If the changes break your code, it automatically rolls them back.

## Checking Performance

Want to see exactly what the AI is doing behind the scenes? 

Every action the autonomous loop takes is recorded in `.loop/timing.log`. You can easily read this data using our built-in analyzer tool:

### Get a simple, human-readable summary of what the loop has been doing:
```
python -m agent.timing_analyze
```

### See only the 5 slowest tasks:
```
python -m agent.timing_analyze --top-n 5
```

If the loop ever seems stuck, you can force it to dump a live status report into `runtime.log` by finding the process and sending a `SIGUSR1` signal.
