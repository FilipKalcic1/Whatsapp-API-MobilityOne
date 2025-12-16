"""
AI Service - Production Ready (v2.0)

Handles communication with Azure OpenAI:
1. Intent analysis with function calling
2. Conversation history management
3. Robust error handling

Workflow:
1. Build messages array (system + history + current)
2. Include tool definitions if available
3. Call OpenAI Chat Completions
4. Parse response (tool call or text)
"""

import json
import structlog
from typing import List, Dict, Any, Optional

from openai import AsyncAzureOpenAI
from config import get_settings

settings = get_settings()
logger = structlog.get_logger("ai")

# Initialize OpenAI client
client = AsyncAzureOpenAI(
    azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
    api_key=settings.AZURE_OPENAI_API_KEY,
    api_version=settings.AZURE_OPENAI_API_VERSION
)


async def analyze_intent(
    history: List[Dict],
    current_text: str = None,
    tools: List[Dict] = None,
    system_instruction: str = None
) -> Dict[str, Any]:
    """
    Analyze user intent and decide on action.
    
    Args:
        history: Conversation history
        current_text: Current user message (None if continuing from tool result)
        tools: Available tool definitions in OpenAI format
        system_instruction: System prompt with context
    
    Returns:
        {
            "tool": tool_name or None,
            "parameters": {} if tool call,
            "tool_call_id": str if tool call,
            "raw_tool_calls": list if tool call,
            "response_text": str if text response
        }
    """
    # Build messages
    messages = _build_messages(history, current_text, system_instruction)
    
    # Prepare API call
    call_args = {
        "model": settings.AZURE_OPENAI_DEPLOYMENT_NAME,
        "messages": messages,
        "temperature": 0.3,  # Lower = more deterministic
        "max_tokens": 1000
    }
    
    # Add tools if available
    if tools:
        call_args["tools"] = tools
        call_args["tool_choice"] = "auto"
        logger.debug(f"Sending {len(tools)} tools to AI")
    
    try:
        response = await client.chat.completions.create(**call_args)
        
        # Validate response
        if not response.choices:
            logger.error("Empty response from OpenAI")
            return _text_response("Greška: Prazan odgovor od AI servisa.")
        
        message = response.choices[0].message
        
        # Check for tool call
        if message.tool_calls and len(message.tool_calls) > 0:
            tool_call = message.tool_calls[0]
            
            # Parse arguments
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON in tool arguments", 
                             raw=tool_call.function.arguments)
                arguments = {}
            
            logger.info("AI decided to call tool", 
                       tool=tool_call.function.name,
                       params=list(arguments.keys()))
            
            return {
                "tool": tool_call.function.name,
                "parameters": arguments,
                "tool_call_id": tool_call.id,
                "raw_tool_calls": _serialize_tool_calls(message.tool_calls),
                "response_text": None
            }
        
        # Text response
        text = message.content or "Nisam razumio. Možeš li pojasniti?"
        logger.info("AI text response", length=len(text))
        
        return _text_response(text)
        
    except Exception as e:
        logger.error("OpenAI API error", error=str(e))
        return _text_response("Isprike, došlo je do greške. Pokušaj ponovno.")


def _build_messages(
    history: List[Dict],
    current_text: str,
    system_instruction: str
) -> List[Dict]:
    """
    Build messages array for OpenAI.
    
    Handles complex history including tool calls by flattening them
    to avoid "broken chain" errors.
    """
    messages = []
    
    # System prompt
    if system_instruction:
        messages.append({
            "role": "system",
            "content": system_instruction
        })
    
    # Process history
    for msg in history:
        role = msg.get("role")
        content = msg.get("content")
        
        if role == "user":
            messages.append({
                "role": "user",
                "content": content or ""
            })
        
        elif role == "assistant":
            # Check if this was a tool call
            if msg.get("tool_calls"):
                # Flatten tool call to text for safety
                tool_name = msg["tool_calls"][0].get("function", {}).get("name", "unknown")
                messages.append({
                    "role": "assistant",
                    "content": f"[Pozvao sam alat: {tool_name}]"
                })
            else:
                messages.append({
                    "role": "assistant",
                    "content": content or ""
                })
        
        elif role == "tool":
            # Convert tool result to system message for context
            tool_name = msg.get("name", "tool")
            messages.append({
                "role": "system",
                "content": f"[Rezultat od {tool_name}]: {content}"
            })
    
    # Current message
    if current_text:
        messages.append({
            "role": "user",
            "content": current_text
        })
    
    return messages


def _serialize_tool_calls(tool_calls) -> List[Dict]:
    """
    Serialize tool calls for storage in history.
    Handles both Pydantic models and dicts.
    """
    result = []
    
    for tc in tool_calls:
        if hasattr(tc, "model_dump"):
            result.append(tc.model_dump())
        elif hasattr(tc, "dict"):
            result.append(tc.dict())
        elif isinstance(tc, dict):
            result.append(tc)
        else:
            # Manual extraction
            result.append({
                "id": getattr(tc, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(tc.function, "name", ""),
                    "arguments": getattr(tc.function, "arguments", "{}")
                }
            })
    
    return result


def _text_response(text: str) -> Dict[str, Any]:
    """Create a text-only response."""
    return {
        "tool": None,
        "parameters": {},
        "tool_call_id": None,
        "raw_tool_calls": None,
        "response_text": text
    }