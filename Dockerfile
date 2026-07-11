FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY llm_os/ llm_os/
COPY ui/ ui/
COPY .streamlit/ .streamlit/

# Default command runs the kernel API; the UI service overrides this in compose.
EXPOSE 8000 8501
CMD ["uvicorn", "llm_os.api:app", "--host", "0.0.0.0", "--port", "8000"]
