"""Provider-agnostic tool calling. A `ToolSession` holds one conversation in the provider's own message format:
OpenAI-compatible chat.completions (`tools` / `tool_calls` / role "tool") or Gemini on Vertex AI (function
declarations, automatic function calling disabled, function responses). The assistant loop only sees `ModelTurn`s.
The `complete()` protocol used by every other task is untouched."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .client import LiteLLMClient, VertexGeminiClient


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema (object)
    fn: Callable[..., dict]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    arg_error: str | None = None


@dataclass
class ModelTurn:
    text: str
    calls: list[ToolCall] = field(default_factory=list)


class ToolSession(Protocol):
    model: str

    def step(self, allow_tools: bool = True) -> ModelTurn: ...

    def add_tool_results(self, results: list[tuple[ToolCall, dict]]) -> None: ...

    def add_user(self, text: str) -> None: ...


def _parse_args(raw: Any) -> tuple[dict, str | None]:
    if isinstance(raw, dict):
        return raw, None
    if raw in (None, ""):
        return {}, None
    try:
        obj = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        return {}, f"arguments are not valid JSON: {exc}"
    return (obj, None) if isinstance(obj, dict) else ({}, "arguments must be a JSON object")


class OpenAIToolSession:
    """chat.completions with `tools`; tool results go back as role 'tool' messages."""

    def __init__(self, client: Any, model: str, system: str, history: list[dict], tools: list[ToolSpec]):
        self._client = client
        self.model = model
        self._tools = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                          "parameters": t.parameters}} for t in tools]
        self.messages: list[dict] = [{"role": "system", "content": system}] + \
            [{"role": m["role"], "content": m["content"]} for m in history]
        self._temperature = True

    def _create(self, **kw):
        if self._temperature:
            try:
                return self._client.chat.completions.create(temperature=0, **kw)
            except Exception as exc:  # some models only accept the default temperature
                if "temperature" not in str(exc).lower():
                    raise
                self._temperature = False
        return self._client.chat.completions.create(**kw)

    def step(self, allow_tools: bool = True) -> ModelTurn:
        resp = self._create(model=self.model, messages=self.messages, tools=self._tools,
                            tool_choice="auto" if allow_tools else "none")
        msg = resp.choices[0].message
        calls = []
        for tc in (msg.tool_calls or []) if allow_tools else []:
            args, err = _parse_args(tc.function.arguments)
            calls.append(ToolCall(tc.id, tc.function.name, args, err))
        entry: dict = {"role": "assistant", "content": msg.content or ""}
        if calls:
            entry["tool_calls"] = [{"id": c.id, "type": "function",
                                    "function": {"name": c.name, "arguments": json.dumps(c.args)}} for c in calls]
        self.messages.append(entry)
        return ModelTurn(msg.content or "", calls)

    def add_tool_results(self, results: list[tuple[ToolCall, dict]]) -> None:
        for call, res in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id,
                                  "content": json.dumps(res, default=str, separators=(",", ":"))})

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


class GeminiToolSession:
    """Gemini (google-genai) function calling with automatic function calling disabled. The model's own content
    (including thought signatures) is appended verbatim before the function responses."""

    def __init__(self, client: Any, types: Any, model: str, system: str, history: list[dict],
                 tools: list[ToolSpec]):
        self._client = client
        self._t = types
        self.model = model
        self._system = system
        self._tool = types.Tool(function_declarations=[
            types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
            for t in tools])
        self.contents: list = [types.Content(role="model" if m["role"] == "assistant" else "user",
                                             parts=[types.Part.from_text(text=m["content"])]) for m in history]

    def step(self, allow_tools: bool = True) -> ModelTurn:
        t = self._t
        cfg = t.GenerateContentConfig(
            system_instruction=self._system, temperature=0, tools=[self._tool],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            tool_config=t.ToolConfig(function_calling_config=t.FunctionCallingConfig(
                mode="AUTO" if allow_tools else "NONE")))
        resp = self._client.models.generate_content(model=self.model, contents=self.contents, config=cfg)
        fcs = list(getattr(resp, "function_calls", None) or []) if allow_tools else []
        cands = getattr(resp, "candidates", None) or []
        content = getattr(cands[0], "content", None) if cands else None
        calls = []
        for i, fc in enumerate(fcs):
            args, err = _parse_args(dict(fc.args or {}))
            calls.append(ToolCall(fc.id or f"call_{len(self.contents)}_{i}", fc.name, args, err))
        if content is not None and content.parts:
            text = "".join(p.text for p in content.parts if getattr(p, "text", None) and not getattr(p, "thought", False))
            self.contents.append(content)
        else:
            text = "" if calls else (getattr(resp, "text", None) or "")
            parts = [t.Part.from_function_call(name=c.name, args=c.args) for c in calls] or \
                [t.Part.from_text(text=text)]
            self.contents.append(t.Content(role="model", parts=parts))
        return ModelTurn("" if calls else text, calls)

    def add_tool_results(self, results: list[tuple[ToolCall, dict]]) -> None:
        t = self._t
        parts = []
        for call, res in results:
            payload = json.loads(json.dumps(res, default=str))
            parts.append(t.Part(function_response=t.FunctionResponse(
                id=None if call.id.startswith("call_") else call.id, name=call.name, response=payload)))
        self.contents.append(t.Content(role="user", parts=parts))

    def add_user(self, text: str) -> None:
        self.contents.append(self._t.Content(role="user", parts=[self._t.Part.from_text(text=text)]))


def make_tool_session(llm: Any, system: str, history: list[dict], tools: list[ToolSpec]) -> ToolSession | None:
    """A tool-calling session for the configured client, or None if the client cannot call tools."""
    if llm is None:
        return None
    if hasattr(llm, "tool_session"):  # test doubles
        return llm.tool_session(system, history, tools)
    if isinstance(llm, LiteLLMClient):
        return OpenAIToolSession(llm._client, llm.model, system, history, tools)
    if isinstance(llm, VertexGeminiClient):
        return GeminiToolSession(llm._client, llm._types, llm.model, system, history, tools)
    return None
