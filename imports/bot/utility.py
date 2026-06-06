# TinyChat (c) 2026 WarWar <somethingstrenge@gmail.com>
# This file is part of TinyChat.
# TinyChat is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License v3.

import re
import html
import math
import logging
from PIL import Image

from imports.config import CONFIG, get_provider
from imports.providers.gemini import GeminiProvider
from imports.providers.lm_studio import LMStudioProvider

logger = logging.getLogger(__name__)

def markdown_to_html(text: str) -> str:
    """Convert simple CommonMark-like syntax to Telegram-safe HTML.

    - Preserves fenced code blocks (```...```) and inline code (`...`).
    - Converts nested `_**bold**_` and `**_italic_**` combos to nested tags.
    - Converts `**...**` and `__...__` to `<b>`, and `_..._` and `*...*` to `<i>`.
    This is a pragmatic converter for typical assistant outputs; it intentionally
    HTML-escapes text before injecting tags to avoid accidental HTML.
    """
    if not text:
        return text

    # 1) LaTeX arrow replacements
    latex_reps = {
        r"\\rightarrow\b": "→",
        r"\\leftarrow\b": "←",
        r"\\Rightarrow\b": "⇒",
        r"\\Leftarrow\b": "⇐",
        r"\\leftrightarrow\b": "↔",
        r"\\Leftrightarrow\b": "⇔",
        r"\\longrightarrow\b": "⟶",
        r"\\longleftarrow\b": "⟵",
        r"\\implies\b": "⟹",
        r"\\iff\b": "⟺",
        r"\\to\b": "→",
    }
    for k, v in latex_reps.items():
        text = re.sub(k, v, text)

    # 2) Math symbol replacements
    math_symbols = {
        r"\\sim\b": "~",
        r"\\le\b": "≤",
        r"\\ge\b": "≥",
        r"\\leq\b": "≤",
        r"\\geq\b": "≥",
        r"\\gg\b": "≫",
        r"\\ll\b": "≪",
        r"\\approx\b": "≈",
        r"\\neq\b": "≠",
        r"\\%": "%",
        r"\\times\b": "×",
        r"\\div\b": "÷",
        r"\\pm\b": "±",
        r"\\cdot\b": "·",
        r"\\degree\b": "°",
    }
    for k, v in math_symbols.items():
        text = re.sub(k, v, text)

    # 3) \text{...} replacement
    text = re.sub(r"\\text\{([^}]+)\}", r"\1", text)

    # 4) Clean up double dollars
    text = re.sub(r"\$\$(.*?)\$\$", r"\1", text, flags=re.DOTALL)

    # 5) Clean up single dollars
    def _cb_single_dollar(m):
        content = m.group(1)
        contains_math_op = any(op in content for op in ('+', '-', '*', '/', '=', '<', '>', '^', '_', '~', '≤', '≥', '≈', '≠', '±', '·', '°', '×', '÷', '%'))
        is_short = len(content.strip()) <= 5 and ' ' not in content.strip()
        
        words = set(re.findall(r'\b[a-zA-Z]+\b', content.lower()))
        has_stopwords = bool(words & {'and', 'or', 'the', 'is', 'of', 'to', 'for', 'in', 'with', 'on', 'at', 'by', 'an', 'a'})
        
        if contains_math_op or (is_short and len(content.strip()) > 0) or (not has_stopwords and len(content.strip()) > 0):
            return content
        else:
            return f"${content}$"
            
    text = re.sub(r"\$([^\$]+?)\$", _cb_single_dollar, text)


    # 1) Extract fenced code blocks
    code_blocks: dict[str, str] = {}
    def _cb_code(m):
        key = f"@@CODEBLOCK{len(code_blocks)}@@"
        code_blocks[key] = m.group(1)
        return key
    text = re.sub(r"```(.*?)```", _cb_code, text, flags=re.DOTALL)

    # 2) Extract inline code
    inline_codes: dict[str, str] = {}
    def _cb_inline(m):
        key = f"@@INLCODE{len(inline_codes)}@@"
        inline_codes[key] = m.group(1)
        return key
    text = re.sub(r"`([^`]+?)`", _cb_inline, text)

    # 3) Escape remaining text to HTML
    text = html.escape(text)

    # 3.5) Typography and list/header adjustments (outside code spans)
    # Replace long em-dash with shorter en-dash
    text = text.replace('—', '–')

    # Replace Markdown header markers (#, ##, etc.) at start of line with a chevron
    # Add a newline before it for better readability
    # Use multiline flag so ^ matches line starts
    text = re.sub(r'(?m)^(\s*)#+\s+', r'\n\1➤ ', text)

    # Replace unordered list markers '*' or '-' at start of line with a bullet '•'
    text = re.sub(r'(?m)^([ \t]*)[\*-][ \t]+', r"\1• ", text)

    # 4) Convert nested combinations first
    # _**text**_  -> <i><b>text</b></i>
    text = re.sub(r"(?<![A-Za-z0-9])_(\*\*(.+?)\*\*)_(?![A-Za-z0-9])", lambda m: f"<i><b>{m.group(2)}</b></i>", text, flags=re.DOTALL)
    # **_text_** -> <b><i>text</i></b>
    text = re.sub(r"\*\*(\_(.+?)\_)\*\*", lambda m: f"<b><i>{m.group(2)}</i></b>", text, flags=re.DOTALL)

    # 5) Simple strong/italic replacements
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<b>{m.group(1)}</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", lambda m: f"<b>{m.group(1)}</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<![A-Za-z0-9])_(.+?)_(?![A-Za-z0-9])", lambda m: f"<i>{m.group(1)}</i>", text, flags=re.DOTALL)
    text = re.sub(r"(?<![A-Za-z0-9])\*(.+?)\*(?![A-Za-z0-9])", lambda m: f"<i>{m.group(1)}</i>", text, flags=re.DOTALL)

    # 6) Reinsert inline code (escaped inside code tag)
    for k, v in inline_codes.items():
        text = text.replace(k, f"<code>{html.escape(v)}</code>")

    # 7) Reinsert code blocks
    for k, v in code_blocks.items():
        text = text.replace(k, f"<pre><code>{html.escape(v)}</code></pre>")

    return text

def compress_image_to_2mp(image_path: str) -> None:
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            total_pixels = width * height
            if total_pixels > 2000000:
                scale = math.sqrt(2000000 / total_pixels)
                new_width = int(width * scale)
                new_height = int(height * scale)
                resample_filter = getattr(Image, 'Resampling', Image).LANCZOS
                resized_img = img.resize((new_width, new_height), resample=resample_filter)
                resized_img.save(image_path, format=img.format or 'JPEG')
    except Exception as e:
        logging.getLogger(__name__).warning(f"Error compressing image {image_path}: {e}")

def build_provider():
    """Instantiate the correct AI provider based on app_config.yaml → models.main_model."""
    main_model_cfg = (CONFIG.get('models') or {}).get('main_model') or {}
    provider_name = main_model_cfg.get('provider', '')

    if provider_name == 'gemini':
        prov_cfg = get_provider('gemini') or {}
        model = main_model_cfg.get('model') or prov_cfg.get('default_model') or 'gemini-2.0-flash'
        return GeminiProvider(default_model=model)
    elif provider_name == 'lmstudio':
        prov_cfg = get_provider('lmstudio') or {}
        # default to localhost for LM Studio
        url = prov_cfg.get('url', 'http://localhost:1234')
        # LM Studio will use any model if no directly scpecified, so we can default to a generic name here
        model = prov_cfg.get('default_model', 'default_model')
        return LMStudioProvider(url, model)
    else:
        raise ValueError(f"Unsupported provider specified in config: {provider_name}")