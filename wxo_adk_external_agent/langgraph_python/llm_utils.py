# llm_utils.py
import json
import logging
import time
import traceback
import uuid
from typing import List, Dict, Any

from ibm_watsonx_ai import APIClient, Credentials
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage, BaseMessage, ToolCall
from langchain_ibm import ChatWatsonx
from langgraph.prebuilt import create_react_agent

import config
from models import Message, AIToolCall, Function, ModelName
from token_utils import get_access_token

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def _create_model_instance(model: str, parm_overrides=None):
    """Creates and returns the appropriate LLM instance based on configuration."""
    if parm_overrides is None:
        parm_overrides = {}
    defaults = {
        'temperature': 0,
        'streaming': True
    }
    defaults.update(parm_overrides)

    provider = config.AI_SERVICES_PROVIDER

    if 'bedrock' in provider:
        # Converse API client — required for Amazon Nova, compatible with Claude too.
        try:
            from langchain_aws import ChatBedrockConverse
            import boto3
            from botocore.config import Config
            # Resilience knobs come from config (env-tunable). "adaptive" mode adds client-side
            # rate limiting + more retries with backoff; a read timeout ensures a hung call fails
            # fast (and is retried) instead of stalling the request. See config.py.
            retry_config = Config(
                retries={
                    'max_attempts': config.BEDROCK_MAX_ATTEMPTS,
                    'mode': 'adaptive'
                },
                read_timeout=config.BEDROCK_READ_TIMEOUT,
                connect_timeout=10
            )
            bedrock_client = boto3.client(
                service_name="bedrock-runtime",
                region_name=config.AWS_REGION,
                config=retry_config
            )
            return ChatBedrockConverse(model=model if model else config.BEDROCK_MODEL_ID,
                                       client=bedrock_client,
                                       temperature=defaults.get('temperature', 0),
                                       max_tokens=config.BEDROCK_MAX_TOKENS)
        except ImportError:
            raise ValueError(
                "Bedrock provider selected but 'langchain[aws]' not installed. "
                "Run: pip install 'langchain[aws]' bedrock-agentcore"
            )

    if 'ollama' in provider:
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model if model else ModelName.ollama_qwen2_5vl_7b,
                          **defaults)

    if 'deepseek' in provider:
        if not config.DEEPSEEK_API_KEY:
            raise ValueError("DEEPSEEK_API_KEY is not set")

        from langchain_deepseek import ChatDeepSeek
        return ChatDeepSeek(model=model if model else ModelName.deepseek_chat,
                            api_key=config.DEEPSEEK_API_KEY,
                            **defaults)

    if 'openai' in provider:
        if not config.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is not set")

        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model if model else ModelName.openai_gpt_4_o_mini,
                          api_key=config.OPENAI_API_KEY,
                          **defaults)

    # Default to Watsonx.ai
    if config.WATSONX_SPACE_ID:
        client_model_instance = APIClient(
            credentials=Credentials(url=config.WATSONX_URL, token=get_access_token(config.WATSONX_API_KEY)),
            space_id=config.WATSONX_SPACE_ID)
    elif config.WATSONX_PROJECT_ID:
        client_model_instance = APIClient(
            credentials=Credentials(url=config.WATSONX_URL, token=get_access_token(config.WATSONX_API_KEY)),
            project_id=config.WATSONX_PROJECT_ID)
    else:
        raise ValueError("You must either set WATSONX_SPACE_ID or WATSONX_PROJECT_ID")
    model_instance = ChatWatsonx(model_id=model,
                                 watsonx_client=client_model_instance,
                                 **defaults)
    return model_instance


def convert_messages_to_langgraph_format(messages: List[Message]) -> Dict[str, Any]:
    conv_messages = []
    max_message_length = 50000
    for msg in messages:
        if msg.content and len(msg.content) > max_message_length:
            msg.content = msg.content[:max_message_length]
        role = msg.role
        logging.debug(f"Converting input message of type {role}")
        new_message = None
        if role.lower() == 'user' or role.lower() == 'human':
            new_message = HumanMessage(content=msg.content)
        if role.lower() == 'system':
            new_message = SystemMessage(content=msg.content)
        if role.lower() == 'assistant':
            content = ''
            additional_kwargs = {}
            if msg.content:
                content = msg.content
            if msg.tool_calls:
                # Convert list of AIToolCall messages to langchain ToolCall message
                langchain_tool_calls = []
                for index, tool_call in enumerate(msg.tool_calls):
                    name = tool_call.function.name
                    args = tool_call.function.arguments
                    id = tool_call.id
                    langchain_tool_calls.append(ToolCall(name=name, args=args, id=id, type='tool'))

                new_message = AIMessage(content=content, tool_calls=langchain_tool_calls,
                                        additional_kwargs=additional_kwargs)
            else:
                new_message = AIMessage(content=content, additional_kwargs=additional_kwargs)
        if role.lower() == 'tool':
            tool_call_id = msg.tool_call_id
            content = msg.content
            name = None
            new_message = ToolMessage(content=content, name=name, tool_call_id=tool_call_id)
        if new_message:
            conv_messages.append(new_message)
    return {
        "messages": conv_messages
    }


def convert_response_to_messages(response: dict) -> List[Message]:
    messages = []
    for msg in response['messages']:
        role = 'not found'
        if msg.type:
            role = msg.type
        logging.info(f"Processing role {role}")
        tool_calls = None
        if 'tool_calls' in msg:
            tool_calls = msg['tool_calls']
        if msg.additional_kwargs:
            additional_kwargs = msg.additional_kwargs
            if 'tool_calls' in additional_kwargs:
                tool_calls = []
                for tool_call_data in additional_kwargs['tool_calls']:
                    function_arguments = tool_call_data['function']['arguments']
                    if isinstance(function_arguments, str):
                        function_arguments = json.loads(function_arguments)
                    tool_call = AIToolCall(
                        id=tool_call_data['id'],
                        function=Function(
                            arguments=function_arguments,
                            name=tool_call_data['function']['name']
                        ),
                        type=tool_call_data['type']
                    )
                    tool_calls.append(tool_call)
        content = ""
        if msg.content:
            content = msg.content
        id = None
        if msg.id:
            id = msg.id
        name = None
        if 'name' in msg:
            name = msg['name']
        if msg.name:
            name = msg.name
        tool_call_id = None
        if 'tool_call_id' in msg:
            tool_call_id = msg['tool_call_id']
        if role == 'tool' and msg.tool_call_id:
            tool_call_id = msg.tool_call_id
        if role == 'human':
            message = Message(
                role='user',
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id
            )
        elif role == 'ai':
            message = Message(
                role='assistant',
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id
            )
        else:
            message = Message(
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id
            )
        messages.append(message)
    return messages


def get_llm_sync(messages: List[Message], model: str, _thread_id: str, tools):
    logging.info(f"LLM Synchronous call using model {model} and tools {tools}")
    # Create the model instance
    model_instance = _create_model_instance(model)
    logging.info(f"Starting with input messages: {messages}")
    inputs = convert_messages_to_langgraph_format(messages)
    validate_chat_history(inputs["messages"])
    logging.info(f"Calling langgraph with input: {inputs}")
    if tools:
        graph = create_react_agent(model_instance, tools=tools)
        response = graph.invoke(inputs)
    else:
        graph = model_instance
        response = graph.invoke(inputs['messages'])
    logging.info(f"Response: {response}")
    if hasattr(response, 'content'):
        results = response.content
        message = Message(
            role='ai',
            content=results,
            tool_calls=None,
            tool_call_id=None
        )
        messages = [message.model_dump()]
    else:
        results = response["messages"][-1].content
    return results, messages


def format_resp(struct):
    return "data: " + json.dumps(struct) + "\n\n"


def validate_chat_history(messages: List[BaseMessage]):
    tool_call_ids = set()
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tool_call in msg.tool_calls:
                if isinstance(tool_call, dict):
                    tool_call_ids.add(tool_call.get('id'))
                else:
                    tool_call_ids.add(getattr(tool_call, 'id', None))

    for msg in messages:
        if isinstance(msg, ToolMessage):
            if msg.tool_call_id in tool_call_ids:
                tool_call_ids.remove(msg.tool_call_id)

    for tool_call_id in tool_call_ids:
        logging.info(f"Fixing input that had no tool response for tool_call_id {tool_call_id}")
        placeholder_message = ToolMessage(
            content="Tool call failed or no response received.",
            tool_call_id=tool_call_id,
            name="unknown"
        )
        messages.append(placeholder_message)


async def get_llm_stream(messages: List[Message], model: str, thread_id: str, tools):
    if tools:
        use_tools = True
    else:
        use_tools = False
    send_tool_events = True
    logging.info(f"LLM Stream with tools {tools}")
    model_init_overrides = {'temperature': 0, 'streaming': True}
    if not thread_id:
        logging.warning("Warning no thread_id specified in input")
        thread_id = ""
    # Create the model instance
    model_instance = _create_model_instance(model, model_init_overrides)
    if use_tools:
        graph = create_react_agent(model_instance, tools=tools)
    else:
        graph = create_react_agent(model_instance, tools=[])
    inputs = ""
    accumulated_contents = ""
    try:
        inputs = convert_messages_to_langgraph_format(messages)
        validate_chat_history(inputs["messages"])
        async for event in graph.astream_events(inputs, version="v2"):
            kind = event["event"]
            logging.debug(f"event = {event}")
            if kind == "on_chat_model_stream":
                content = event["data"]["chunk"].content
                if content:
                    if isinstance(content, str):
                        current_timestamp = int(time.time())
                        struct = {
                            "id": str(uuid.uuid4()),
                            "object": "thread.message.delta",
                            "created": current_timestamp,
                            "thread_id": thread_id,
                            "model": model,
                            "choices": [
                                {
                                    "delta": {
                                        "content": content,
                                        "role": "assistant",
                                    }
                                }
                            ],
                        }
                        event_content = format_resp(struct)
                        logging.debug("Sending event content: " + event_content)
                        accumulated_contents += content
                        yield event_content
                    elif isinstance(content, list):
                        for item in content:
                            if 'type' in item:
                                if item['type'] == 'text':
                                    yield item['text']
                                elif item['type'] == 'tool_use':
                                    logging.debug("tool_use")
                                    logging.debug(f"{str(item)}")
                                else:
                                    logging.debug("Received item of type " + item['type'])
            elif kind == "on_tool_start":
                printmsg = f"Starting tool: {event['name']} with inputs: {event['data'].get('input')} run_id: {event['run_id']}"
                logging.debug(printmsg)
                current_timestamp = int(time.time())
                step_details = {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": event['run_id'],
                            "name": event['name'],
                            "args": event['data'].get('input')
                        }
                    ]
                }
                struct = {
                    "id": str(uuid.uuid4()),
                    "object": "thread.run.step.delta",
                    "thread_id": thread_id,
                    "model": model,
                    "created": current_timestamp,
                    "choices": [
                        {
                            "delta": {
                                "role": "assistant",
                                "step_details": step_details
                            }
                        }
                    ],
                }
                thinking_step_details = {
                    "type": "thinking",
                    "content": "The user's question will require an internet search using a search tool."
                }
                thinking_struct = {
                    "id": str(uuid.uuid4()),
                    "object": "thread.run.step.delta",
                    "thread_id": thread_id,
                    "model": model,
                    "created": current_timestamp,
                    "choices": [
                        {
                            "delta": {
                                "role": "assistant",
                                "step_details": thinking_step_details
                            }
                        }
                    ]
                }
                thinking_event_content = format_resp(thinking_struct)
                logging.info("Sending thinking event content: " + thinking_event_content)
                if send_tool_events:
                    yield thinking_event_content
                event_content = format_resp(struct)
                logging.info("Sending tool call event content: " + event_content)
                if send_tool_events:
                    yield event_content
            elif kind == "on_tool_end":
                tool_name = event.get('name', '')
                logging.info(f"Event on_tool_end for tool: {tool_name}")
                output = event.get('data', {}).get('output', {})
                content = ''
                if output and output['content']:
                    content = output['content']
                run_id = event['run_id']
                logging.info(f"Tool output for run {run_id} was: {content}")
                tool_call_id = run_id  # Better matches tool response with tool request
                if output and output['tool_call_id']:
                    tool_call_id = output['tool_call_id']
                current_timestamp = int(time.time())
                step_details = {
                    "type": "tool_response",
                    "name": event['name'],
                    "tool_call_id": tool_call_id,
                    "content": content
                }
                struct = {
                    "id": str(uuid.uuid4()),
                    "object": "thread.run.step.delta",
                    "thread_id": thread_id,
                    "model": model,
                    "created": current_timestamp,
                    "choices": [
                        {
                            "delta": {
                                "role": "assistant",
                                "step_details": step_details
                            }
                        }
                    ],
                }
                event_content = format_resp(struct)
                logging.info("Sending tool response event content: " + event_content)
                if send_tool_events:
                    yield event_content
            elif kind == "on_chat_model_start":
                logging.debug(f"Received event type: on_chat_model_start")
            elif kind == "on_chat_model_end":
                logging.debug(f"Received event type: on_chat_model_end")
            else:
                logging.debug("Received event type: " + kind)
            yield ""

        if accumulated_contents:
            logging.info("Final streamed content:\n" + accumulated_contents)

    except Exception as e:
        logging.error(f"Exception {str(e)}")
        traceback.print_exc()
        logging.error(f"Exception was with inputs {str(inputs)}")
        yield f"Error: {str(e)}\n"
