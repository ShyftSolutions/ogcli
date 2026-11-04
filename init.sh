#!/bin/bash
#
# WMS CLI Setup Script
# Installs the CLI and enables shell completion
#

# Colors (disabled if not a terminal)
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    BLUE='\033[0;34m'
    BOLD='\033[1m'
    NC='\033[0m' # No Color
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    BOLD=''
    NC=''
fi

# Symbols
CHECK="${GREEN}✓${NC}"
CROSS="${RED}✗${NC}"
ARROW="${BLUE}→${NC}"
WARN="${YELLOW}⚠${NC}"

# Print functions
info() { echo -e "${ARROW} $1"; }
success() { echo -e "${CHECK} $1"; }
warn() { echo -e "${WARN} $1"; }
error() { echo -e "${CROSS} ${RED}$1${NC}"; }

# Get the directory where this script is located (works even if called from elsewhere)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
VENV_BIN="$VENV_DIR/bin"

# Header
echo
echo -e "${BOLD}╔════════════════════════════════════╗${NC}"
echo -e "${BOLD}║         WMS CLI Setup              ║${NC}"
echo -e "${BOLD}╚════════════════════════════════════╝${NC}"
echo

# Step 1: Check prerequisites
echo -e "${BOLD}Checking prerequisites...${NC}"

# Check pyproject.toml exists
if [ ! -f "$SCRIPT_DIR/pyproject.toml" ]; then
    error "pyproject.toml not found"
    echo "  Make sure you're running this from the wms-cli directory"
    echo "  or that the script hasn't been moved."
    exit 1
fi
success "Found pyproject.toml"

# Check Python
if ! command -v python3 &> /dev/null; then
    error "python3 not found"
    echo
    echo "  Please install Python 3.10 or later:"
    echo "    macOS:  brew install python3"
    echo "    Ubuntu: sudo apt install python3"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
success "Found Python $PYTHON_VERSION"

# Check Python version is 3.10+
PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]); then
    error "Python 3.10+ required (found $PYTHON_VERSION)"
    exit 1
fi

# Detect shell
SHELL_NAME=$(basename "$SHELL")
success "Detected shell: $SHELL_NAME"

echo

# Step 2: Set up virtual environment
echo -e "${BOLD}Setting up environment...${NC}"

if [ -d "$VENV_DIR" ]; then
    info "Virtual environment already exists"
else
    info "Creating virtual environment..."
    if ! python3 -m venv "$VENV_DIR" 2>&1; then
        error "Failed to create virtual environment"
        exit 1
    fi
    success "Created virtual environment"
fi

# Activate and install
info "Installing ogcli (wms + edr)..."
source "$VENV_BIN/activate"

if ! pip install -e "$SCRIPT_DIR" -q --disable-pip-version-check 2>/dev/null; then
    error "Failed to install wms-cli"
    exit 1
fi
success "Installed wms-cli"

# Verify wms command works
if ! "$VENV_BIN/wms" --help &> /dev/null; then
    error "Installation verification failed"
    exit 1
fi
# Verify edr command works
if ! "$VENV_BIN/edr" --help &> /dev/null; then
    error "EDR installation verification failed"
    exit 1
fi
success "Verified installation (wms + edr)"

echo

# Step 3: Configure shell
echo -e "${BOLD}Configuring shell completion...${NC}"

# Determine config based on shell
case "$SHELL_NAME" in
    zsh)
        CONFIG_FILE="$HOME/.zshrc"
        PATH_LINE="export PATH=\"$VENV_BIN:\$PATH\""
        COMPLETION_LINE='eval "$(_WMS_COMPLETE=zsh_source wms)" && eval "$(_EDR_COMPLETE=zsh_source edr)"'
        ;;
    bash)
        CONFIG_FILE="$HOME/.bashrc"
        PATH_LINE="export PATH=\"$VENV_BIN:\$PATH\""
        COMPLETION_LINE='eval "$(_WMS_COMPLETE=bash_source wms)" && eval "$(_EDR_COMPLETE=bash_source edr)"'

        # Check bash version
        BASH_VER=$(bash -c 'echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}')
        BASH_MAJOR=$(bash -c 'echo ${BASH_VERSINFO[0]}')
        if [ "$BASH_MAJOR" -lt 4 ]; then
            warn "Bash $BASH_VER detected - completion requires Bash 4.4+"
            echo "    macOS ships with old Bash. Options:"
            echo "    1. Use zsh instead (default on macOS): chsh -s /bin/zsh"
            echo "    2. Install newer bash: brew install bash"
            echo
        fi
        ;;
    fish)
        CONFIG_FILE="$HOME/.config/fish/config.fish"
        FISH_COMP_FILE="$HOME/.config/fish/completions/wms.fish"
        PATH_LINE="set -gx PATH $VENV_BIN \$PATH"
        COMPLETION_LINE=""
        ;;
    *)
        warn "Unknown shell: $SHELL_NAME"
        echo
        echo "  Manual setup required. Add to your shell config:"
        echo "    export PATH=\"$VENV_BIN:\$PATH\""
        echo
        echo "  Then see README.md for completion setup."
        exit 0
        ;;
esac

# Check what needs to be added
NEEDS_PATH=false
NEEDS_COMPLETION=false

if ! grep -q "$VENV_BIN" "$CONFIG_FILE" 2>/dev/null; then
    NEEDS_PATH=true
fi

if [ "$SHELL_NAME" = "fish" ]; then
    if [ ! -f "$FISH_COMP_FILE" ]; then
        NEEDS_COMPLETION=true
    fi
else
    if ! grep -q "_WMS_COMPLETE" "$CONFIG_FILE" 2>/dev/null; then
        NEEDS_COMPLETION=true
    fi
fi

# If nothing to do, we're done
if [ "$NEEDS_PATH" = false ] && [ "$NEEDS_COMPLETION" = false ]; then
    success "Shell already configured"
    echo
    echo -e "${BOLD}${GREEN}Setup complete!${NC}"
    echo
    echo "  Run: source $CONFIG_FILE"
    echo "  Then: wms <tab>"
    echo
    exit 0
fi

# Show what will be added
echo
echo "  The following will be added to ${BOLD}$CONFIG_FILE${NC}:"
echo
if [ "$NEEDS_PATH" = true ]; then
    echo "    ${BLUE}# WMS CLI${NC}"
    echo "    ${BLUE}$PATH_LINE${NC}"
fi
if [ "$NEEDS_COMPLETION" = true ] && [ -n "$COMPLETION_LINE" ]; then
    echo "    ${BLUE}$COMPLETION_LINE${NC}"
fi
echo

# Ask for confirmation
read -p "  Add these lines? [Y/n] " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]] && [[ -n $REPLY ]]; then
    echo
    warn "Skipped shell configuration"
    echo
    echo "  To set up manually, add to $CONFIG_FILE:"
    [ "$NEEDS_PATH" = true ] && echo "    $PATH_LINE"
    [ "$NEEDS_COMPLETION" = true ] && [ -n "$COMPLETION_LINE" ] && echo "    $COMPLETION_LINE"
    echo
    exit 0
fi

# Add configuration
if [ "$NEEDS_PATH" = true ]; then
    echo "" >> "$CONFIG_FILE"
    echo "# WMS CLI" >> "$CONFIG_FILE"
    echo "$PATH_LINE" >> "$CONFIG_FILE"
fi

if [ "$SHELL_NAME" = "fish" ]; then
    if [ "$NEEDS_COMPLETION" = true ]; then
        mkdir -p "$(dirname "$FISH_COMP_FILE")"
        export PATH="$VENV_BIN:$PATH"
        _WMS_COMPLETE=fish_source wms > "$FISH_COMP_FILE" 2>/dev/null
        _EDR_COMPLETE=fish_source edr > "${FISH_COMP_FILE/wms.fish/edr.fish}" 2>/dev/null
        success "Created Fish completions (wms + edr)"
    fi
else
    if [ "$NEEDS_COMPLETION" = true ]; then
        echo "$COMPLETION_LINE" >> "$CONFIG_FILE"
    fi
fi

success "Updated $CONFIG_FILE"

echo

# Final message
echo -e "${BOLD}${GREEN}════════════════════════════════════${NC}"
echo -e "${BOLD}${GREEN}  Setup complete!                   ${NC}"
echo -e "${BOLD}${GREEN}════════════════════════════════════${NC}"
echo
echo "  ${BOLD}Next steps:${NC}"
echo
echo "  1. Reload your shell config:"
echo "     ${BLUE}source $CONFIG_FILE${NC}"
echo
echo "  2. Initialize with a server:"
echo "     ${BLUE}wms init https://ogc.shyftwx.com/ogc/WMS${NC}"
echo "     ${BLUE}edr init https://folkweather.com/edr${NC}"
echo
echo "  3. Try tab completion:"
echo "     ${BLUE}wms <tab>${NC}"
echo "     ${BLUE}wms map gfs<tab>${NC}"
echo "     ${BLUE}edr <tab>${NC}"
echo
