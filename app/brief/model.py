"""Subscription-only Codex subprocess boundary. No fallback provider."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path
import signal
import tempfile
import time
from typing import Callable

from jsonschema import Draft202012Validator, FormatChecker


class ModelError(Exception):
    def __init__(self, code: str, metadata: dict | None = None):
        self.metadata = metadata or {}
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ModelRequest:
    instruction: str
    data: dict
    schema: dict
    timeout_s: float = 180


@dataclass(frozen=True)
class ModelReply:
    data: dict
    metadata: dict


def safe_environment() -> dict[str, str]:
    # Explicit allowlist: no API credentials, provider URLs, inherited session IDs.
    return {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}}


async def run_process(argv: list[str], *, stdin: bytes = b'', timeout: float = 180,
                      cwd: str | None = None, env: dict | None = None,
                      limit: int = 1048576) -> tuple[int, bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cwd, env=env, start_new_session=True)
    except FileNotFoundError:
        raise ModelError('cli_missing') from None
    total = 0

    async def read(stream):
        nonlocal total
        chunks = []
        while chunk := await stream.read(8192):
            total += len(chunk)
            if total > limit:
                raise ModelError('output_limit')
            chunks.append(chunk)
        return b''.join(chunks)

    async def communicate():
        async def feed():
            try:
                process.stdin.write(stdin)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
        tasks = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr)), asyncio.create_task(feed())]
        try:
            stdout, stderr, _ = await asyncio.gather(*tasks)
            await process.wait()
            return process.returncode, stdout, stderr
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        return await asyncio.wait_for(communicate(), timeout)
    except TimeoutError:
        raise ModelError('timeout') from None
    finally:
        # Own group only. Cleanup also runs on cancellation and output overflow.
        if process.returncode is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: await asyncio.wait_for(process.wait(), 1)
            except TimeoutError:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                await process.wait()


DISABLED_FEATURES = (
    'shell_tool', 'unified_exec', 'shell_snapshot', 'apps', 'plugins', 'remote_plugin',
    'hooks', 'multi_agent', 'multi_agent_v2', 'browser_use', 'in_app_browser',
    'computer_use', 'image_generation', 'view_image', 'code_mode', 'code_mode_host',
    'skill_search', 'tool_suggest', 'workspace_dependencies', 'memories', 'goals',
    'sleep_tool', 'unbounded_connection_retries',
)


def isolated_args(directory: str, schema_file: str, model: str | None) -> list[str]:
    args = ['codex', 'exec', '--ignore-user-config', '--strict-config', '--ephemeral',
            '--skip-git-repo-check', '--json', '--color', 'never', '-C', directory,
            '--output-schema', schema_file]
    config = ['forced_login_method="chatgpt"', 'model_provider="openai"', 'model_reasoning_effort="low"',
              'approval_policy="never"', 'web_search="disabled"', 'project_doc_max_bytes=0',
              'skills.include_instructions=false', 'features.skip_host_skill_discovery=true',
              'tools.update_plan.enabled=false', 'hide_agent_reasoning=true',
              'history.persistence="none"', 'analytics.enabled=false',
              'default_permissions="brief"',
              'permissions.brief={filesystem={":root"="deny",":minimal"="read",":workspace_roots"="deny"},network={enabled=false}}']
    config.extend(f'features.{name}=false' for name in DISABLED_FEATURES)
    for value in config: args.extend(['-c', value])
    if model: args.extend(['--model', model])
    return args + ['-']


class CodexModel:
    def __init__(self, *, runner: Callable = run_process, model: str | None = "gpt-6-astra"):
        self.runner, self.model = runner, model

    async def generate(self, request: ModelRequest) -> ModelReply:
        env = safe_environment()
        status, out, _ = await self.runner(['codex', 'login', 'status'], env=env, timeout=15)
        if status or b'Logged in using ChatGPT' not in out:
            # Some CLI versions emit login status on stderr; tested below in smoke.
            if not (status == 0 and b'Logged in using ChatGPT' in _):
                raise ModelError('auth')
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='filipp-model-') as directory:
            schema_file = Path(directory) / 'response.schema.json'
            schema_file.write_text(json.dumps(request.schema), encoding='utf-8')
            args = isolated_args(directory, str(schema_file), self.model)
            data = (request.instruction + '\nUSER_DATA_JSON:\n' + json.dumps(request.data, ensure_ascii=False)).encode()
            attempts = []
            for attempt in range(1, 4):
                attempt_start = time.monotonic()
                try:
                    code, stdout, stderr = await self.runner(args, stdin=data, timeout=request.timeout_s, cwd=directory, env=env)
                except ModelError as e:
                    attempts.append({'attempt':attempt,'error':e.code,'duration_s':time.monotonic()-attempt_start})
                    e.metadata={**e.metadata,'attempts':attempts,'attempt':attempt,'provider':'codex_chatgpt','model':self.model}
                    raise
                if code:
                    error = self._classify((stderr + stdout).decode(errors='replace'))
                    attempts.append({'attempt':attempt,'error':error,'duration_s':time.monotonic()-attempt_start})
                    if error == 'temporary' and attempt < 3:
                        await asyncio.sleep((1, 3)[attempt - 1])
                        continue
                    raise ModelError(error, {'attempts':attempts,'attempt':attempt,'provider':'codex_chatgpt','model':self.model})
                try:
                    result, usage = self._parse(stdout, request.schema)
                except ModelError as e:
                    attempts.append({'attempt':attempt,'error':e.code,'duration_s':time.monotonic()-attempt_start})
                    e.metadata={**e.metadata,'attempts':attempts,'attempt':attempt,'provider':'codex_chatgpt','model':self.model}
                    raise
                attempts.append({'attempt':attempt,'error':None,'duration_s':time.monotonic()-attempt_start})
                return ModelReply(result, {'duration_s': time.monotonic()-start, 'attempt': attempt,
                    'model': self.model, 'reasoning_effort':'low', 'attempts':attempts, 'usage': usage, 'provider': 'codex_chatgpt'})
        raise ModelError('execution')

    @staticmethod
    def _classify(text: str) -> str:
        text = text.lower()
        if any(x in text for x in ['usage limit', 'quota', 'rate limit', '429']): return 'quota'
        if any(x in text for x in ['unauthorized', '401', 'not logged', 'authentication']): return 'auth'
        if any(x in text for x in ['502 bad gateway', '503 service unavailable', 'connection reset']): return 'temporary'
        return 'execution'

    @staticmethod
    def _parse(stdout: bytes, schema: dict) -> tuple[dict, dict | None]:
        messages, usage = [], None
        stage = 'event_json'
        recovery_message_start = None
        diagnostic = {'stdout_sha256': hashlib.sha256(stdout).hexdigest(), 'stdout_bytes': len(stdout)}
        try:
            for line_number, line in enumerate(stdout.splitlines(), 1):
                stage = 'event_json'
                diagnostic['line_number'] = line_number
                event = json.loads(line)
                stage = 'event_shape'
                if not isinstance(event, dict): raise ValueError()
                item = event.get('item', {})
                if not isinstance(item, dict): raise ValueError()
                if event.get('type') in ('error', 'turn.failed'):
                    stage = 'cli_event'
                    # CLI emits error events while reconnecting, even when the same
                    # invocation later completes successfully. Never retry here.
                    if event['type'] == 'error' and isinstance(event.get('message'), str) and event['message'].startswith('Reconnecting... '):
                        recovery_message_start = len(messages)
                        continue
                    raise ValueError()
                if event.get('type') == 'item.completed' and item.get('type') == 'agent_message':
                    if not isinstance(item.get('text'), str): raise ValueError()
                    messages.append(item['text'])
                if event.get('type') == 'turn.completed':
                    if recovery_message_start is not None:
                        stage = 'cli_event'
                        if len(messages) <= recovery_message_start: raise ValueError()
                        recovery_message_start = None
                    usage = event.get('usage')
            if recovery_message_start is not None:
                stage = 'cli_event'
                raise ValueError()
            stage = 'missing_message'
            if not messages: raise ValueError()
            stage = 'message_json'
            result = json.loads(messages[-1])
            stage = 'schema'
            errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(result))
            if errors:
                # Schema paths come from our schema, never from user-controlled object keys.
                diagnostic['schema_path'] = list(errors[0].absolute_schema_path)
                diagnostic['validator'] = errors[0].validator
                raise ValueError()
            return result, usage
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
            diagnostic.update(stage=stage, exception_type=type(error).__name__)
            raise ModelError('invalid_response', {'diagnostic': diagnostic}) from None
