from __future__ import annotations

import re

_PATH_PATTERN = re.compile(
    r'(?:[A-Za-z]:[\\/]|/mnt/[a-z]/|/home/|/root/|~/)'
    r'\S*(?:'
    r'\.json|\.xlsx|\.xls|\.db|\.sqlite|\.safetensors|\.ckpt|\.pt|\.pth|'
    r'\.env|\.py|\.ps1|\.bat|\.sh|\.yaml|\.yml|\.cfg|\.ini|\.csv|\.log'
    r')',
    re.IGNORECASE,
)

_SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'\b(?:api[_\s-]?key|token|secret|password|passwd)\s*[:=]\s*\S+', re.I), '***'),
    (re.compile(r'sk-[a-zA-Z0-9]{20,}', re.I), '***'),
    (re.compile(r'bearer\s+[a-zA-Z0-9\-_.]+', re.I), 'Bearer ***'),
    (re.compile(r'\b(?:cookie|csrf|session)\s*[:=]\s*\S+', re.I), '***'),
    (re.compile(r'redis://[^\s]+', re.I), '***'),
    (re.compile(r'\bbalance\.db\b', re.I), '[database]'),
    (re.compile(r'\b(bot_web_mvp\.py|start_uma_local\.ps1|agent\.xlsx)\b', re.I), '[internal file]'),
]

_FILENAME_PATTERN = re.compile(
    r'\b[\w\-]+\.(?:json|xlsx|xls|db|sqlite|safetensors|ckpt|pt|pth|env|ps1|bat|py)\b',
    re.IGNORECASE,
)

_GENERIC_REPLACEMENTS: dict[str, str] = {
    'reading_file': '正在查找合适的资料……',
    'loading_model': '正在准备生成模型……',
    'workflow_file': '正在选择合适的图片工作流……',
    'lora_file': '正在寻找合适的 LoRA……',
    'prompt_file': '正在匹配合适的提示词片段……',
    'database': '正在准备任务数据……',
    'internal_file': '正在处理内部配置……',
}


def sanitize_public_agent_message(text: str) -> str:
    if not text:
        return ""
    result = str(text)

    for pattern, replacement in _SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)

    def _replace_path(match: re.Match[str]) -> str:
        matched = match.group(0).lower()
        if any(ext in matched for ext in ('.safetensors', '.ckpt', '.pt', '.pth')):
            return _GENERIC_REPLACEMENTS['lora_file']
        if any(ext in matched for ext in ('.json', '.xlsx', '.xls', '.csv')):
            if 'workflow' in matched or 'prompt' in matched:
                return _GENERIC_REPLACEMENTS['workflow_file'] if 'workflow' in matched else _GENERIC_REPLACEMENTS['prompt_file']
            return _GENERIC_REPLACEMENTS['reading_file']
        if '.db' in matched or '.sqlite' in matched:
            return _GENERIC_REPLACEMENTS['database']
        if any(ext in matched for ext in ('.py', '.ps1', '.bat', '.sh')):
            return _GENERIC_REPLACEMENTS['internal_file']
        return _GENERIC_REPLACEMENTS['reading_file']

    result = _PATH_PATTERN.sub(_replace_path, result)

    result = _FILENAME_PATTERN.sub(
        lambda m: _GENERIC_REPLACEMENTS.get(
            _classify_filename(m.group(0)), m.group(0)
        ),
        result,
    )

    return result


def _classify_filename(name: str) -> str:
    low = name.lower()
    if any(ext in low for ext in ('.safetensors', '.ckpt', '.pt', '.pth')):
        return 'lora_file'
    if any(ext in low for ext in ('.json', '.xlsx', '.xls')):
        return 'reading_file'
    if any(ext in low for ext in ('.db', '.sqlite')):
        return 'database'
    if any(ext in low for ext in ('.py', '.ps1', '.bat')):
        return 'internal_file'
    return 'reading_file'
