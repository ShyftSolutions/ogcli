"""
Terminal image display utilities.
Supports multiple terminal protocols for displaying PNG images.
"""

import sys
import os
from pathlib import Path
from typing import Optional, Union
from PIL import Image
import base64






def display_image_ascii(image_path: Union[str, Path], max_width: int = 120, color: bool = True):
    """Display image using Unicode half-blocks with dual colors for 2x vertical resolution."""
    img = Image.open(image_path).convert('RGB')

    # Resize to fit terminal width
    # Each character will display 2 vertical pixels, so double the height
    aspect_ratio = img.height / img.width
    new_width = min(max_width, img.width)
    new_height = int(new_width * aspect_ratio)  # No 0.5 factor - we're packing 2 pixels per char

    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
    pixels = list(img.getdata())

    output = ''
    # Process two rows at a time
    for y in range(0, new_height - 1, 2):
        for x in range(new_width):
            # Get top and bottom pixel colors
            top_idx = y * new_width + x
            bottom_idx = (y + 1) * new_width + x

            if top_idx < len(pixels) and bottom_idx < len(pixels):
                top_r, top_g, top_b = pixels[top_idx]
                bottom_r, bottom_g, bottom_b = pixels[bottom_idx]

                # Use upper half block ▀ with dual colors
                # Foreground color = top pixel, Background color = bottom pixel
                output += f'\033[38;2;{top_r};{top_g};{top_b}m\033[48;2;{bottom_r};{bottom_g};{bottom_b}m▀\033[0m'

        output += '\n'

    print(output)


def display_image(
    image_path: Union[str, Path],
    max_width: Optional[int] = None,
):
    """
    Display an image in the terminal using Unicode half-blocks.

    Args:
        image_path: Path to the PNG image file
        max_width: Maximum width in terminal cells/columns (default: 120)
    """
    image_path = Path(image_path)

    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    # Use half-block display
    display_image_ascii(image_path, max_width or 120)


def get_image_info(image_path: Union[str, Path]) -> dict:
    """Get basic information about an image file."""
    img = Image.open(image_path)
    return {
        'width': img.width,
        'height': img.height,
        'format': img.format,
        'mode': img.mode,
        'size_bytes': Path(image_path).stat().st_size
    }
