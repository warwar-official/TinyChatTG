# TinyChat (c) 2026 WarWar <somethingstrenge@gmail.com>
# This file is part of TinyChat.
# TinyChat is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License v3.

import os
import sys
import subprocess
from pathlib import Path

# Needed directories
DIRS = [
    "data/state",
    "data/configs",
    "data/files/documents",
    "data/files/images",
    "data/audio",
    "data/mcp",
    "data/memory/db",
    "data/memory/model",
    "logs",
]

# Default tools config
DEFAULT_TOOLS_YAML = """tools:
  file_list:
    description: List all files owned by the user (documents and images). Shows file
      name, type, origin (loaded or created), and last modified time.
    handler: file_list.list_files
    require_approval: false
    schema:
      properties:
        start_id:
          type: integer
          description: ID of the first file to return (default 0).
        count:
          type: integer
          description: Number of files to return (max 20, default 20).
      type: object
    visible: true
    allow_summarizing: false
  file_read_lines:
    description: 'Read lines from a file. Use this for reading text documents. It
      returns lines prefixed with line numbers (e.g. ''1: line content'').'
    handler: file_read_lines.read_file_lines
    require_approval: false
    schema:
      properties:
        file_name:
          type: string
          description: The name of the file to read.
        start_id:
          type: integer
          description: Start line number (1-based, inclusive, default 1).
        count:
          type: integer
          description: Number of lines to read (max 50, default 50).
      required:
      - file_name
      type: object
    visible: true
    allow_summarizing: false
  file_grep:
    description: Search for a specific text string inside a file. Use this to quickly
      locate keywords in large text files.
    handler: file_grep.grep_file
    require_approval: false
    schema:
      properties:
        file_name:
          type: string
          description: The name of the file to search.
        query:
          type: string
          description: The exact text to search for.
      required:
      - file_name
      - query
      type: object
    visible: true
    allow_summarizing: false
  file_find_by_name:
    description: Search for files by name. Matches if the query is a substring of
      the file name.
    handler: file_find_by_name.find_by_name
    require_approval: false
    schema:
      properties:
        query:
          type: string
          description: Substring to search for in file names.
        limit:
          type: integer
          description: Max number of results to return (default 10).
      required:
      - query
      type: object
    visible: true
    allow_summarizing: false
  file_find_by_similarity:
    description: Search for files (documents or images) by semantic similarity to
      their model-generated descriptions.
    handler: file_find_by_similarity.find_by_similarity
    require_approval: false
    schema:
      properties:
        query:
          type: string
          description: Search query describing the content you're looking for.
        top_k:
          type: integer
          description: Number of top matches to return (default 5).
      required:
      - query
      type: object
    visible: true
    allow_summarizing: true
  file_create:
    description: Create a new empty text document.
    handler: file_create.create_file
    require_approval: false
    schema:
      properties:
        file_name:
          type: string
          description: Name of the new file to create. Must not already exist.
      required:
      - file_name
      type: object
    visible: true
    allow_summarizing: false
  file_add_lines:
    description: Append lines to the end of a file. If the file is 'loaded' (read-only),
      it will automatically be duplicated to an editable 'created' copy first.
    handler: file_add_lines.add_lines
    require_approval: false
    schema:
      properties:
        file_name:
          type: string
          description: Name of the file.
        lines:
          type: array
          items:
            type: string
          description: List of strings to append as lines.
      required:
      - file_name
      - lines
      type: object
    visible: true
    allow_summarizing: false
  file_replace_lines:
    description: Replace lines in a file starting at a specific line number. If the
      file is 'loaded' (read-only), it will automatically be duplicated to an editable
      'created' copy first.
    handler: file_replace_lines.replace_lines
    require_approval: false
    schema:
      properties:
        file_name:
          type: string
          description: Name of the file.
        line_id:
          type: integer
          description: The 1-based line number to start replacing at. If beyond EOF,
            it appends.
        lines:
          type: array
          items:
            type: string
          description: List of strings to insert.
      required:
      - file_name
      - line_id
      - lines
      type: object
    visible: true
    allow_summarizing: false
  file_send:
    description: Send a created file back to the user. Note Only files
      with origin='created' can be sent. 'loaded' files cannot be sent back.
    handler: file_send.send_file
    require_approval: false
    schema:
      properties:
        file_name:
          type: string
          description: Name of the file to send.
      required:
      - file_name
      type: object
    visible: true
    allow_summarizing: true
  scratchpad_add_record:
    description: Append a new line to your private scratchpad. The scratchpad is always visible in your system context.
    handler: scratchpad_add.add_record
    require_approval: false
    schema:
      properties:
        text:
          type: string
          description: Text of the new scratchpad line.
      required:
      - text
      type: object
    visible: true
  scratchpad_remove_record:
    description: Remove a line from the scratchpad by its line number (1-based).
    handler: scratchpad_remove.remove_record
    require_approval: false
    schema:
      properties:
        record_id:
          type: integer
          description: Line number to remove (1-based).
      required:
      - record_id
      type: object
    visible: true
  scratchpad_update_record:
    description: Replace the text of an existing scratchpad line.
    handler: scratchpad_update.update_record
    require_approval: false
    schema:
      properties:
        record_id:
          type: integer
          description: Line number to update (1-based).
        text:
          type: string
          description: New text for the line.
      required:
      - record_id
      - text
      type: object
    visible: true
  scratchpad_wipe_records:
    description: Remove all lines from the scratchpad.
    handler: scratchpad_wipe.wipe_records
    require_approval: false
    schema:
      properties: {}
      type: object
    visible: true
"""


DEFAULT_APP_CONFIG = """providers:
  lmstudio:
    name: lmstudio
    url: null
    description: LM Studio local OpenAI-compatible endpoint
    default_model: default_model

  gemini:
    name: gemini
    description: "Google Gemini REST API provider"
    default_model: "gemma-4-31b-it"

models:
  main_model:
    provider: gemini
    model: gemma-4-31b-it
  backup_model:
    provider: gemini
    model: gemma-4-26b-a4b-it

mcp:
  TinyMCP:
    enabled: true
    command: ""
    args: []
    env: {}

memory:
  model_path: data/memory/model
  db_path: data/memory/db
  embedding_model: intfloat/multilingual-e5-large
  max_tool_response_chars: 15000

bot:
  auth_code_ttl_seconds: 60
  max_auth_failures_per_ttl: 5
  start_spam_limit_per_minute: 5
  start_spam_ban_seconds: 3600
  last_messages_tail: 10
  max_messages: 25
  max_tool_iterations: 10
  summarize_tool_results: true

logging:
  debug: true
  logs_path: logs

concurrency:
  max_concurrent: 1
  requests_per_minute: 15
  primary_workers: 1

whisper:
  url: https://api.groq.com/openai/v1/audio/transcriptions
  rpm: 20
  max_size: 25000000
"""


def print_status(msg, status="INFO"):
    colors = {
        "INFO": "\033[94m",
        "SUCCESS": "\033[92m",
        "ERROR": "\033[91m",
        "WARN": "\033[93m",
        "RESET": "\033[0m"
    }
    print(f"{colors.get(status, '')}[{status}] {msg}{colors['RESET']}")


def in_venv():
    """Check if we are running inside a virtual environment."""
    return sys.prefix != sys.base_prefix


def create_venv_and_relaunch():
    """Create a .venv and re-run this script inside it."""
    venv_dir = Path(".venv")
    if not venv_dir.exists():
        print_status("Virtual environment '.venv' not found. Creating...", "INFO")
        try:
            subprocess.check_call([sys.executable, "-m", "venv", ".venv"])
            print_status("Created virtual environment.", "SUCCESS")
        except subprocess.CalledProcessError as e:
            print_status(f"Failed to create virtual environment: {e}", "ERROR")
            sys.exit(1)
    else:
        print_status("Virtual environment '.venv' already exists.", "INFO")

    # Determine executable path
    if os.name == 'nt':
        python_exe = venv_dir / "Scripts" / "python.exe"
    else:
        python_exe = venv_dir / "bin" / "python"

    if not python_exe.exists():
        print_status(f"Could not find Python executable at {python_exe}", "ERROR")
        sys.exit(1)

    print_status("Relaunching setup script inside the virtual environment...", "INFO")
    
    # Relaunch script
    try:
        sys.exit(subprocess.call([str(python_exe), __file__] + sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(1)


def install_dependencies():
    """Install dependencies from requirements.txt"""
    req_file = Path("requirements.txt")
    if not req_file.exists():
        print_status("requirements.txt not found. Skipping dependency installation.", "WARN")
        return

    print_status("Installing dependencies...", "INFO")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req_file)])
        print_status("Dependencies installed successfully.", "SUCCESS")
    except subprocess.CalledProcessError as e:
        print_status(f"Failed to install dependencies: {e}", "ERROR")
        sys.exit(1)


def create_folder_structure():
    """Create necessary application directories."""
    print_status("Creating folder structure...", "INFO")
    for d in DIRS:
        path = Path(d)
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            print_status(f"Created directory: {d}", "SUCCESS")
        else:
            print_status(f"Directory already exists: {d}", "INFO")


def create_tools_config():
    """Create app_tools.yaml if it doesn't exist."""
    tools_file = Path("data/mcp/app_tools.yaml")
    if not tools_file.exists():
        print_status("Creating default app_tools.yaml...", "INFO")
        tools_file.parent.mkdir(parents=True, exist_ok=True)
        with open(tools_file, "w", encoding="utf-8") as f:
            f.write(DEFAULT_TOOLS_YAML)
        print_status("Created app_tools.yaml.", "SUCCESS")
    else:
        print_status("app_tools.yaml already exists.", "INFO")


def cache_fastembed_model():
    """Download and cache the fastembed model based on app_config.yaml."""
    print_status("Initializing fastembed model to cache it locally...", "INFO")
    try:
        import yaml
        from fastembed import TextEmbedding
        
        model_name = "BAAI/bge-small-en-v1.5" # Default fallback
        config_path = Path("data/configs/app_config.yaml")
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                if config and "memory" in config and "embedding_model" in config["memory"]:
                    model_name = config["memory"]["embedding_model"]
        
        print_status(f"Using embedding model: {model_name}", "INFO")
        model_cache_dir = str(Path("data/memory/model").absolute())
        
        # Setting the environment variable fastembed expects for caching
        os.environ["FASTEMBED_CACHE_PATH"] = model_cache_dir
        
        # Instantiate to trigger download
        _ = TextEmbedding(model_name=model_name, cache_dir=model_cache_dir)
        print_status(f"Fastembed model '{model_name}' cached successfully.", "SUCCESS")
    except ImportError as e:
        print_status(f"Missing dependency: {e}. Skipping model cache.", "WARN")
    except Exception as e:
        print_status(f"Failed to cache fastembed model: {e}", "ERROR")

def ensure_pandoc():
    """Ensure pandoc is available. Prefer pypandoc's downloader if installed."""
    import pypandoc
    try:
        ver = pypandoc.get_pandoc_version()
        print_status(f"Pandoc already available: {ver}", "INFO")
        return
    except Exception:
        print_status("Pandoc binary not found via pypandoc. This is required for document conversions. Install it manually.", "WARN")
    print_status("pypandoc not installed; pandoc installation skipped.", "WARN")


def create_app_config():
    cfg_file = Path("data/configs/app_config.yaml")
    if not cfg_file.exists():
        print_status("Creating default app_config.yaml...", "INFO")
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg_file, "w", encoding="utf-8") as f:
            f.write(DEFAULT_APP_CONFIG)
        print_status("Created app_config.yaml.", "SUCCESS")
    else:
        print_status("app_config.yaml already exists.", "INFO")


def main():
    print_status("Starting TinyChatTG Setup", "INFO")
    
    # 1. Ensure we are in a virtual environment
    if not in_venv():
        create_venv_and_relaunch()
        return

    # If we reached here, we are INSIDE the virtual environment.
    print_status(f"Running inside virtual environment: {sys.prefix}", "INFO")

    # 2. Install dependencies
    install_dependencies()

    # 3. Create folder structure
    create_folder_structure()

    # 4. Create default config files
    create_tools_config()
    create_app_config()

    # 4.5 Ensure pandoc available for document conversions
    ensure_pandoc()

    # 5. Initialize/Cache Models
    cache_fastembed_model()

    print_status("Setup completed successfully!", "SUCCESS")
    print_status("To activate the virtual environment, run:", "INFO")
    if os.name == 'nt':
        print("    .venv\\Scripts\\activate")
    else:
        print("    source .venv/bin/activate")


if __name__ == "__main__":
    main()
