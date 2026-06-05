import os
import sys
import subprocess
from pathlib import Path

# Needed directories
DIRS = [
    "data/state",
    "data/configs",
    "data/images",
    "data/mcp",
    "data/memory/db",
    "data/memory/model",
    "logs",
]

# Default tools config
DEFAULT_TOOLS_YAML = """tools:
  remember_info:
    description: Manually add memory entry to the memory store to remember facts or user preferences. Text must be between 50 and 350 characters long and contain alphabetical letters.
    handler: remember_info.add_memory
    require_approval: false
    schema:
      properties:
        text:
          type: string
        title:
          type: string
      required:
      - text
      - title
      type: object
    visible: true
  recall_info:
    description: Search the user's memory store to recall facts, user preferences, or past conversation summaries
    handler: recall_info.search_memory
    require_approval: false
    schema:
      properties:
        query:
          type: string
          description: Search query
        limit:
          type: integer
          description: Max number of results to return
      required:
      - query
      type: object
    visible: true
      type: object
    visible: true
  file_list:
    description: Show list of files in user's directory from newest to oldest. Returns total count of files and range shown.
    handler: file_list.list_files
    require_approval: false
    schema:
      properties:
        start_id:
          type: integer
          description: Index to start listing files from (0-indexed).
        count:
          type: integer
          description: Max number of files to return (maximum 20).
      type: object
    visible: true
  file_read_lines:
    description: Return lines from a requested text file in the user's directory. Returns total lines, range shown, lines content, and end of file mark if reached.
    handler: file_read_lines.read_file_lines
    require_approval: false
    schema:
      properties:
        file_name:
          type: string
          description: The exact name of the file to read (must not contain path separators or traversal marks).
        start_id:
          type: integer
          description: The 1-based line number to start reading from.
        count:
          type: integer
          description: Max number of lines to return (maximum 50).
      required:
      - file_name
      type: object
    visible: true
  file_search:
    description: Search for a query string in a file from the user's directory. Returns line numbers and snippets around match (max 50 results).
    handler: file_search.search_file
    require_approval: false
    schema:
      properties:
        file_name:
          type: string
          description: The exact name of the file to search in (must not contain path separators or traversal marks).
        query:
          type: string
          description: The query sequence to search for.
      required:
      - file_name
      - query
      type: object
    visible: true
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
