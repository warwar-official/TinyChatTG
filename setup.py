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
    visible: true
    description: "Store an important fact or preference about the user into the long-term memory."
    permissions: "auto"
    _provider: "app"
    handler: "remember_info"
    schema:
      type: "object"
      properties:
        fact:
          type: "string"
          description: "The fact to remember"
        title:
          type: "string"
          description: "Title of the fact"
      required: ["fact", "title"]

  recall_info:
    visible: true
    description: "Search long-term memory for facts and preferences."
    permissions: "auto"
    _provider: "app"
    handler: "memory_search"
    schema:
      type: "object"
      properties:
        query:
          type: "string"
          description: "Search query"
      required: ["query"]
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
