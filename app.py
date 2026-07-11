import os
import time
import json
import requests
import streamlit as st
from ollama import Client

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://llm-engine:11434")
SCRATCHPAD_DIR = "/app/scratchpad"
DEFAULT_MODEL = "Orgnational/minicpm5-1b"

# Page configurations
st.set_page_config(
    page_title="ChatGPT - LLM OS",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Light Theme CSS - Black text, White background
st.markdown("""
<style>
    /* Light Theme Core */
    .stApp {
        background-color: #ffffff;
        color: #1a1a1a;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    
    /* All text elements - black */
    .stApp p, .stApp span, .stApp label, .stApp div, .stApp li, .stApp h1, .stApp h2, .stApp h3, .stApp h4 {
        color: #1a1a1a !important;
    }
    
    /* Center Layout Elements */
    .welcome-container {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        text-align: center;
        margin-top: 5vh;
        margin-bottom: 5vh;
    }
    
    .chat-logo {
        background-color: #10a37f;
        color: white;
        border-radius: 50%;
        width: 60px;
        height: 60px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 32px;
        font-weight: bold;
        margin-bottom: 20px;
    }
    
    .welcome-title {
        font-size: 32px;
        font-weight: 600;
        margin-bottom: 30px;
        color: #1a1a1a !important;
    }

    /* Sidebar Styling - light */
    section[data-testid="stSidebar"] {
        background-color: #f7f7f8 !important;
        border-right: 1px solid #e5e5e5;
    }
    
    section[data-testid="stSidebar"] .stMarkdown, 
    section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] span,
    section[data-testid="stSidebar"] label {
        color: #1a1a1a !important;
    }
    
    /* Buttons */
    .stButton > button {
        background-color: #ffffff !important;
        color: #1a1a1a !important;
        border: 1px solid #e5e5e5 !important;
        border-radius: 12px !important;
    }
    .stButton > button:hover {
        background-color: #f7f7f8 !important;
        border-color: #10a37f !important;
    }
    
    /* Chat messages */
    [data-testid="stChatMessage"] {
        background-color: #f7f7f8 !important;
        border-radius: 12px !important;
        color: #1a1a1a !important;
    }
    
    /* Input Styling - white bg, black text, big font */
    [data-testid="stChatInput"] {
        border: 1px solid #d9d9d9 !important;
        background-color: #ffffff !important;
        border-radius: 28px !important;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08) !important;
    }
    [data-testid="stChatInput"] textarea,
    .stChatInputContainer textarea {
        background-color: #ffffff !important;
        color: #000000 !important;
        -webkit-text-fill-color: #000000 !important;
        font-size: 20px !important;
        font-weight: 500 !important;
        border: none !important;
        caret-color: #000000 !important;
    }
    [data-testid="stChatInput"] textarea::placeholder,
    .stChatInputContainer textarea::placeholder {
        color: #000000 !important;
        -webkit-text-fill-color: #000000 !important;
        font-size: 20px !important;
        font-weight: 500 !important;
    }
    
    /* Select box */
    .stSelectbox > div > div {
        background-color: #ffffff !important;
        color: #1a1a1a !important;
        border-color: #e5e5e5 !important;
    }
    
    /* Code blocks */
    .stCode, code {
        background-color: #f7f7f8 !important;
        color: #1a1a1a !important;
    }
    
    /* Divider */
    hr {
        border-color: #e5e5e5 !important;
    }
</style>
""", unsafe_allow_html=True)

# Helper function to connect to Ollama
@st.cache_resource
def get_ollama_client():
    return Client(host=OLLAMA_HOST)

# Check connection
def check_ollama_connection():
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False

# Load local models
def get_local_models(client):
    try:
        models = client.list()
        return [m['model'] for m in models.get('models', [])]
    except Exception:
        return []

# Validate connection
if not check_ollama_connection():
    st.error(f"❌ Cannot connect to Ollama service at {OLLAMA_HOST}. Please make sure container 'llm_engine_cpu' is running.")
    st.stop()

client = get_ollama_client()
local_models = get_local_models(client)

# Initialize Session State Messages
if "messages" not in st.session_state:
    st.session_state.messages = []
if "clicked_suggestion" not in st.session_state:
    st.session_state.clicked_suggestion = None

# Sidebar
with st.sidebar:
    st.markdown("""
    <div style='display: flex; align-items: center; gap: 10px; margin-bottom: 20px;'>
        <div style='background-color: #10a37f; width: 30px; height: 30px; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-weight: bold; color: white;'>OS</div>
        <span style='font-size: 18px; font-weight: 600; color: #1a1a1a;'>LLM OS Sidebar</span>
    </div>
    """, unsafe_allow_html=True)
    
    # Model Selector
    st.subheader("🤖 Active LLM Model")
    if not local_models:
        st.warning("No models loaded.")
        selected_model = DEFAULT_MODEL
    else:
        default_index = 0
        for idx, m in enumerate(local_models):
            if DEFAULT_MODEL in m:
                default_index = idx
                break
        selected_model = st.selectbox("Select Model:", local_models, index=default_index, label_visibility="collapsed")
        
    st.divider()
    
    # Actions
    st.subheader("🛠️ Operations")
    if st.button("➕ New Chat", use_container_width=True):
        st.session_state.messages = []
        st.session_state.clicked_suggestion = None
        st.rerun()
        
    st.divider()
    
    # Sandbox view
    st.subheader("📂 Sandbox Storage (`./scratchpad`)")
    if os.path.exists(SCRATCHPAD_DIR):
        files = os.listdir(SCRATCHPAD_DIR)
        if files:
            for file in files:
                st.code(f"📄 {file}", language="text")
        else:
            st.caption("Sandbox directory is empty.")
    else:
        st.caption("Sandbox directory not initialized.")

# Main Layout
# Show ChatGPT Landing Page if conversation is empty
if len(st.session_state.messages) == 0:
    st.markdown("""
    <div class="welcome-container">
        <div class="chat-logo">🤖</div>
        <div class="welcome-title">How can I help you today?</div>
    </div>
    """, unsafe_allow_html=True)
    
    # Suggestion Cards
    suggestions = [
        {"title": "Write a Python script", "desc": "to perform basic arithmetic and file operations"},
        {"title": "Explain neural networks", "desc": "in simple terms with an analogy"},
        {"title": "Design a tool", "desc": "for checking system battery or hardware stats"},
        {"title": "Create a command line", "desc": "instruction to compress files inside Docker"}
    ]
    
    # Display columns for cards
    cols = st.columns(2)
    for idx, card in enumerate(suggestions):
        with cols[idx % 2]:
            # Styled Card with Streamlit button inside to capture clicks
            if st.button(f"💡 **{card['title']}**\n{card['desc']}", use_container_width=True):
                st.session_state.clicked_suggestion = f"{card['title']} {card['desc']}"
                st.rerun()

# Process suggestion clicks
if st.session_state.clicked_suggestion:
    user_query = st.session_state.clicked_suggestion
    st.session_state.clicked_suggestion = None  # Reset
    st.session_state.messages.append({"role": "user", "content": user_query})
    
    # Immediately trigger generation block
    with st.chat_message("user"):
        st.markdown(user_query)
        
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        message_placeholder.markdown("Thinking...")
        try:
            response = client.generate(model=selected_model, prompt=user_query)
            full_response = response.get('response', '')
            message_placeholder.markdown(full_response)
            st.session_state.messages.append({"role": "assistant", "content": full_response})
            
            # Save history to sandbox
            os.makedirs(SCRATCHPAD_DIR, exist_ok=True)
            history_file = os.path.join(SCRATCHPAD_DIR, "chat_history.json")
            with open(history_file, "w") as f:
                json.dump(st.session_state.messages, f, indent=4)
        except Exception as e:
            message_placeholder.error(f"Inference error: {e}")
            st.session_state.messages.append({"role": "assistant", "content": f"ERROR: {e}"})
    st.rerun()

# Display active chat messages
if len(st.session_state.messages) > 0:
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

# Bottom Chat Input Box
if prompt := st.chat_input("Message ChatGPT..."):
    # Display user query
    st.chat_message("user").markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})
    
    # Generate response
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        message_placeholder.markdown("Thinking...")
        try:
            response = client.generate(model=selected_model, prompt=prompt)
            full_response = response.get('response', '')
            message_placeholder.markdown(full_response)
            st.session_state.messages.append({"role": "assistant", "content": full_response})
            
            # Save history to sandbox
            os.makedirs(SCRATCHPAD_DIR, exist_ok=True)
            history_file = os.path.join(SCRATCHPAD_DIR, "chat_history.json")
            with open(history_file, "w") as f:
                json.dump(st.session_state.messages, f, indent=4)
        except Exception as e:
            message_placeholder.error(f"Inference error: {e}")
            st.session_state.messages.append({"role": "assistant", "content": f"ERROR: {e}"})
    
    st.rerun()
