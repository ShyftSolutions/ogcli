#!/bin/bash
#
# ogcli Global Install Script
# Installs the wms and edr commands globally so they work from anywhere
#

set -e

# Colors
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    BLUE='\033[0;34m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' BOLD='' NC=''
fi

CHECK="${GREEN}✓${NC}"
CROSS="${RED}✗${NC}"
ARROW="${BLUE}→${NC}"
WARN="${YELLOW}⚠${NC}"

info() { echo -e "${ARROW} $1"; }
success() { echo -e "${CHECK} $1"; }
warn() { echo -e "${WARN} $1"; }
error() { echo -e "${CROSS} ${RED}$1${NC}"; exit 1; }

# Install location
INSTALL_DIR="$HOME/.wms-cli"
BIN_DIR="$HOME/.local/bin"

# Header
echo
echo -e "${BOLD}╔════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   ogcli Global Install (wms+edr)   ║${NC}"
echo -e "${BOLD}╚════════════════════════════════════╝${NC}"
echo

# Get source directory
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$SOURCE_DIR/pyproject.toml" ]; then
    error "pyproject.toml not found. Run this from the ogcli directory."
fi

# Check Python
if ! command -v python3 &> /dev/null; then
    error "python3 not found. Please install Python 3.10+"
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
success "Found Python $PYTHON_VERSION"

# Create install directory
echo
echo -e "${BOLD}Installing to ${INSTALL_DIR}...${NC}"

if [ -d "$INSTALL_DIR" ]; then
    info "Removing old installation..."
    rm -rf "$INSTALL_DIR"
fi

mkdir -p "$INSTALL_DIR"

# Create virtual environment
info "Creating virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"

# Install package
info "Installing ogcli (wms + edr)..."
"$INSTALL_DIR/venv/bin/pip" install -q --disable-pip-version-check "$SOURCE_DIR"

# Verify installation
if ! "$INSTALL_DIR/venv/bin/wms" --help &> /dev/null; then
    error "Installation failed"
fi
if ! "$INSTALL_DIR/venv/bin/edr" --help &> /dev/null; then
    error "Installation failed (edr)"
fi
success "Installed ogcli (wms + edr)"

# Create bin directory
mkdir -p "$BIN_DIR"

# Create wrapper scripts
info "Creating wms and edr commands..."
cat > "$BIN_DIR/wms" << 'WRAPPER'
#!/bin/bash
exec "$HOME/.wms-cli/venv/bin/wms" "$@"
WRAPPER
cat > "$BIN_DIR/edr" << 'WRAPPER'
#!/bin/bash
exec "$HOME/.wms-cli/venv/bin/edr" "$@"
WRAPPER
chmod +x "$BIN_DIR/wms" "$BIN_DIR/edr"
success "Created $BIN_DIR/wms and $BIN_DIR/edr"

# Detect shell and config file
SHELL_NAME=$(basename "$SHELL")
case "$SHELL_NAME" in
    zsh)  CONFIG_FILE="$HOME/.zshrc" ;;
    bash) CONFIG_FILE="$HOME/.bashrc" ;;
    fish) CONFIG_FILE="$HOME/.config/fish/config.fish" ;;
    *)    CONFIG_FILE="" ;;
esac

# Check if bin dir is in PATH
echo
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo -e "${BOLD}Adding $BIN_DIR to PATH...${NC}"

    if [ -n "$CONFIG_FILE" ]; then
        if ! grep -q "$BIN_DIR" "$CONFIG_FILE" 2>/dev/null; then
            echo "" >> "$CONFIG_FILE"
            echo "# ogcli (wms + edr)" >> "$CONFIG_FILE"
            if [ "$SHELL_NAME" = "fish" ]; then
                echo "set -gx PATH $BIN_DIR \$PATH" >> "$CONFIG_FILE"
            else
                echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$CONFIG_FILE"
            fi
            success "Added PATH to $CONFIG_FILE"
        fi
    else
        warn "Add this to your shell config:"
        echo "    export PATH=\"$BIN_DIR:\$PATH\""
    fi
else
    success "PATH already configured"
fi

# Shell completion
echo
echo -e "${BOLD}Setting up shell completion...${NC}"

case "$SHELL_NAME" in
    zsh)
        COMP_LINE='eval "$(_WMS_COMPLETE=zsh_source wms)"'
        if ! grep -q "_WMS_COMPLETE" "$CONFIG_FILE" 2>/dev/null; then
            echo "$COMP_LINE" >> "$CONFIG_FILE"
            success "Added wms completion to $CONFIG_FILE"
        else
            success "wms completion already configured"
        fi
        COMP_LINE_EDR='eval "$(_EDR_COMPLETE=zsh_source edr)"'
        if ! grep -q "_EDR_COMPLETE" "$CONFIG_FILE" 2>/dev/null; then
            echo "$COMP_LINE_EDR" >> "$CONFIG_FILE"
            success "Added edr completion to $CONFIG_FILE"
        else
            success "edr completion already configured"
        fi
        ;;
    bash)
        COMP_LINE='eval "$(_WMS_COMPLETE=bash_source wms)"'
        if ! grep -q "_WMS_COMPLETE" "$CONFIG_FILE" 2>/dev/null; then
            echo "$COMP_LINE" >> "$CONFIG_FILE"
            success "Added wms completion to $CONFIG_FILE"
        else
            success "wms completion already configured"
        fi
        COMP_LINE_EDR='eval "$(_EDR_COMPLETE=bash_source edr)"'
        if ! grep -q "_EDR_COMPLETE" "$CONFIG_FILE" 2>/dev/null; then
            echo "$COMP_LINE_EDR" >> "$CONFIG_FILE"
            success "Added edr completion to $CONFIG_FILE"
        else
            success "edr completion already configured"
        fi
        ;;
    fish)
        FISH_COMP="$HOME/.config/fish/completions/wms.fish"
        mkdir -p "$(dirname "$FISH_COMP")"
        PATH="$BIN_DIR:$PATH" _WMS_COMPLETE=fish_source wms > "$FISH_COMP" 2>/dev/null
        FISH_COMP_EDR="$HOME/.config/fish/completions/edr.fish"
        PATH="$BIN_DIR:$PATH" _EDR_COMPLETE=fish_source edr > "$FISH_COMP_EDR" 2>/dev/null
        success "Created Fish completions (wms + edr)"
        ;;
    *)
        warn "Manual completion setup needed. See README.md"
        ;;
esac

# Done
echo
echo -e "${BOLD}${GREEN}════════════════════════════════════${NC}"
echo -e "${BOLD}${GREEN}  Installation complete!            ${NC}"
echo -e "${BOLD}${GREEN}════════════════════════════════════${NC}"
echo
echo "  ${BOLD}To start using wms and edr:${NC}"
echo
echo "  1. Reload your shell:"
echo "     ${BLUE}source $CONFIG_FILE${NC}"
echo "     ${BLUE}# or open a new terminal${NC}"
echo
echo "  2. Initialize with a server:"
echo "     ${BLUE}wms init https://ogc.shyftwx.com/ogc/WMS${NC}"
echo "     ${BLUE}edr init https://folkweather.com/edr${NC}"
echo
echo "  3. Start using:"
echo "     ${BLUE}wms layers${NC}"
echo "     ${BLUE}wms map gfs temp f 6h${NC}"
echo "     ${BLUE}edr collections${NC}"
echo "     ${BLUE}edr position hrrr surface -c -105,40${NC}"
echo "     ${BLUE}wms <tab>${NC}"
echo
echo "  ${BOLD}Install location:${NC} $INSTALL_DIR"
echo "  ${BOLD}Commands:${NC} $BIN_DIR/wms, $BIN_DIR/edr"
echo
