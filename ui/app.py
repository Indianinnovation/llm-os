"""LLM OS web console — a thin client over the kernel API.

This UI never talks to the model directly: every prompt goes through
the kernel's routed /chat endpoint, so what you see here is exactly
what any client of the OS would get, tool routing and audit included.
"""

import os

import requests
import streamlit as st

KERNEL_URL = os.environ.get("KERNEL_URL", "http://localhost:8000")
SCRATCHPAD_DIR = os.environ.get("SCRATCHPAD_DIR", "scratchpad")

st.set_page_config(
    page_title="LLM OS — Private Agentic Kernel",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
    .stApp { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .welcome-container { text-align: center; margin: 6vh 0 4vh 0; }
    .chat-logo {
        background: #0f766e; color: white; border-radius: 16px;
        width: 64px; height: 64px; display: inline-flex;
        align-items: center; justify-content: center; font-size: 34px;
        margin-bottom: 16px;
    }
    .welcome-title { font-size: 30px; font-weight: 650; }
    .welcome-sub { font-size: 15px; opacity: 0.7; margin-top: 6px; }
</style>
""",
    unsafe_allow_html=True,
)


def kernel_get(path: str, timeout: int = 5):
    try:
        response = requests.get(f"{KERNEL_URL}{path}", timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def kernel_chat(prompt: str):
    response = requests.post(
        f"{KERNEL_URL}/chat", json={"prompt": prompt}, timeout=300
    )
    response.raise_for_status()
    return response.json()


health = kernel_get("/health")
if health is None:
    st.error(
        f"Cannot reach the LLM OS kernel at {KERNEL_URL}. "
        "Start it with: `uvicorn llm_os.api:app --port 8000` "
        "(or `docker compose up`)."
    )
    st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = []
if "pending_prompt" not in st.session_state:
    st.session_state.pending_prompt = None

with st.sidebar:
    st.markdown("### 🧠 LLM OS")
    st.caption("Private, local-first agentic kernel. Zero egress.")

    engine_ok = health.get("engine") == "ok"
    memory_info = health.get("memory", {})
    memory_line = (
        f"🧠 {memory_info.get('records', 0)} records"
        if memory_info.get("enabled")
        else "disabled"
    )
    st.markdown(
        f"**Engine:** {'🟢' if engine_ok else '🔴'} {health.get('engine')}  \n"
        f"**Model:** `{health.get('active_model')}`  \n"
        f"**Memory:** {memory_line}"
    )
    st.divider()

    st.markdown("**🔧 Registered tools**")
    for tool in kernel_get("/tools") or []:
        badge = "" if tool.get("source", "builtin") == "builtin" else f" · _{tool['source']}_"
        st.markdown(f"- `{tool['name']}`{badge}")
    st.divider()

    if st.button("➕ New chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

    st.markdown("**📂 Sandbox files**")
    if os.path.isdir(SCRATCHPAD_DIR):
        files = sorted(os.listdir(SCRATCHPAD_DIR))
        if files:
            for name in files:
                st.code(name, language="text")
        else:
            st.caption("Sandbox is empty.")
    else:
        st.caption("Sandbox not initialized.")
    st.divider()

    st.markdown("**🧾 Audit log**")
    audit = kernel_get("/audit?n=5")
    if audit:
        chain_ok = audit.get("chain_valid")
        st.caption(
            ("✅ Hash chain verified" if chain_ok else "⚠️ CHAIN BROKEN — log was modified")
        )
        for record in reversed(audit.get("records", [])):
            st.caption(f"`{record['ts']}` · {record['event']} · {record.get('tool', '')}")


def render_trace(trace: list, memories: list) -> None:
    if memories:
        with st.expander(f"🧠 Paged in {len(memories)} memories"):
            for m in memories:
                st.markdown(f"- *({m['ts']})* {m['text']}")
    for step in trace:
        icon = "✅" if step["status"] == "success" else "⚠️"
        with st.expander(
            f"{icon} Routed to `{step['tool']}` · audit `{step.get('audit_id', '-')}`"
        ):
            st.json({"params": step["params"], "result": step["result"]})


def render_message(message: dict) -> None:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        render_trace(message.get("trace", []), message.get("memories", []))


if not st.session_state.messages:
    st.markdown(
        """
    <div class="welcome-container">
        <div class="chat-logo">🧠</div>
        <div class="welcome-title">LLM OS</div>
        <div class="welcome-sub">Everything runs on this machine. Nothing leaves it.</div>
    </div>
    """,
        unsafe_allow_html=True,
    )
    suggestions = [
        "What is 4539 multiplied by 23?",
        "Find the hypotenuse of a right triangle with sides 3 and 4",
        "Write a markdown note called project-ideas listing 3 startup ideas for private AI",
        "What is an LLM OS? Answer briefly.",
    ]
    cols = st.columns(2)
    for idx, suggestion in enumerate(suggestions):
        with cols[idx % 2]:
            if st.button(f"💡 {suggestion}", use_container_width=True, key=f"sug{idx}"):
                st.session_state.pending_prompt = suggestion
                st.rerun()

for message in st.session_state.messages:
    render_message(message)

prompt = st.chat_input("Ask LLM OS…")
if prompt is None and st.session_state.pending_prompt:
    prompt = st.session_state.pending_prompt
    st.session_state.pending_prompt = None

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Routing…"):
            try:
                result = kernel_chat(prompt)
                reply = result.get("reply") or "*(empty reply)*"
                st.markdown(reply)
                render_trace(result.get("trace", []), result.get("memories", []))
                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "content": reply,
                        "trace": result.get("trace", []),
                        "memories": result.get("memories", []),
                    }
                )
            except requests.RequestException as exc:
                st.error(f"Kernel request failed: {exc}")
    st.rerun()
