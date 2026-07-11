import os
import sys
import time
import json
import math
import requests
from ollama import Client

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://llm-engine:11434")
SCRATCHPAD_DIR = "/app/scratchpad"
MODEL_NAME = "Orgnational/minicpm5-1b"

# Safe math functions mapping for the calculator tool
SAFE_MATH = {
    'abs': abs,
    'round': round,
    'pow': pow,
    'sqrt': math.sqrt,
    'sin': math.sin,
    'cos': math.cos,
    'tan': math.tan,
    'pi': math.pi,
    'e': math.e,
}

class LLMOSKernel:
    def __init__(self, host=OLLAMA_HOST, model=MODEL_NAME):
        self.client = Client(host=host)
        self.model = model
        self.system_prompt = """
You are the routing kernel of an LLM OS. Your job is to extract raw mathematical strings from user prompts.
Whenever a user asks you to calculate, solve, compute, or evaluate any expression, map it to the calculator tool.

Examples:
- User: "What is 4539 multiplied by 23?" -> {"tool_to_call": "calculator", "parameters": {"expression": "4539 * 23"}}
- User: "Find the hypotenuse if sides are 3 and 4" -> {"tool_to_call": "calculator", "parameters": {"expression": "sqrt(3**2 + 4**2)"}}

Respond ONLY in valid, clean JSON. Do not include markdown tags like ```json or anything else.
"""

    def calculator(self, expression):
        print(f"[Kernel Tool: Calculator] Evaluating expression: '{expression}'")
        try:
            # Evaluate using restricted globals/locals for sandboxing
            result = eval(expression, {"__builtins__": None}, SAFE_MATH)
            return float(result)
        except Exception as e:
            return f"Error executing mathematical expression: {e}"

    def route_and_execute(self, user_prompt):
        print(f"\n[Kernel Router] Processing input: '{user_prompt}'")
        
        # Prepare system message context
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        try:
            response = self.client.chat(model=self.model, messages=messages)
            response_text = response.get('message', {}).get('content', '').strip()
            
            # Clean possible markdown JSON wrappers in LLM outputs
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]
            response_text = response_text.strip()
            
            print(f"[Kernel Router] LLM Intent Decoded: {response_text}")
            try:
                routing_json = json.loads(response_text)
            except json.JSONDecodeError:
                import ast
                try:
                    # Fallback for Python-like single-quoted dict strings
                    routing_json = ast.literal_eval(response_text)
                except Exception:
                    # Final attempt: replace single quotes and parse
                    cleaned_text = response_text.replace("'", '"')
                    routing_json = json.loads(cleaned_text)
            
            tool_name = routing_json.get("tool_to_call")
            params = routing_json.get("parameters", {})
            
            # Support nested structure if the model outputs tool_to_call as a dictionary
            if isinstance(tool_name, dict):
                params = tool_name.get("parameters", tool_name.get("arguments", params))
                tool_name = tool_name.get("name")
            
            # Parse params if stringified
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except Exception:
                    import ast
                    try:
                        params = ast.literal_eval(params)
                    except Exception:
                        pass
            
            if tool_name == "calculator":
                expression = params.get("expression")
                result = self.calculator(expression)
                return {
                    "status": "success",
                    "tool": "calculator",
                    "expression": expression,
                    "result": result
                }
            else:
                return {
                    "status": "unsupported_tool",
                    "message": f"Tool '{tool_name}' is not currently configured."
                }
                
        except json.JSONDecodeError as je:
            return {
                "status": "parse_error",
                "message": f"Failed to parse LLM response as JSON. Raw output: {response_text}"
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Execution failed: {e}"
            }

def wait_for_ollama(url, timeout=60):
    print(f"Connecting to Ollama host: {url}...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(f"{url}/api/tags")
            if response.status_code == 200:
                print("Connected to Ollama engine successfully!")
                return True
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(2)
    print("Error: Timeout waiting for Ollama service to start.")
    return False

def main():
    if not wait_for_ollama(OLLAMA_HOST):
        sys.exit(1)

    kernel = LLMOSKernel()
    
    # Run test suite
    test_prompts = [
        "What is 4539 multiplied by 23?",
        "Find the hypotenuse if sides are 3 and 4"
    ]
    
    results = []
    for prompt in test_prompts:
        res = kernel.route_and_execute(prompt)
        results.append({
            "prompt": prompt,
            "routing_result": res
        })
        print(f"[Kernel Router] Result: {res}")
        time.sleep(1)

    # Write test logs to the sandbox
    os.makedirs(SCRATCHPAD_DIR, exist_ok=True)
    output_path = os.path.join(SCRATCHPAD_DIR, "kernel_output.txt")
    try:
        with open(output_path, "w") as f:
            f.write("=== LLM OS KERNEL ROUTING LOGS ===\n\n")
            for item in results:
                f.write(f"User Prompt: {item['prompt']}\n")
                f.write(f"Outcome: {json.dumps(item['routing_result'], indent=2)}\n")
                f.write("-" * 40 + "\n")
            f.write(f"Generated at: {time.strftime('%Y-%m-%d %H:%M:%S')} UTC\n")
        print(f"\n[Kernel Output] Successfully saved run report to: {output_path}")
    except Exception as e:
        print(f"Failed to write to scratchpad: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
