from __future__ import annotations

import time
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from api.image_inputs import parse_image_edit_request, read_image_sources
from api.support import require_identity, resolve_image_base_url
from services.content_filter import check_request, request_shape, request_text
from services.editable_file_task_service import editable_file_task_service
from services.image_task_service import image_task_service
from services.log_service import LoggedCall
from services.protocol import (
    anthropic_v1_messages,
    openai_v1_chat_complete,
    openai_v1_image_edit,
    openai_v1_image_generations,
    openai_v1_models,
    openai_v1_response,
    openai_search,
)


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    history_disabled: bool = True
    stream: bool | None = None
    background: bool | str = False
    client_task_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    prompt: str | None = None
    n: int | None = None
    stream: bool | None = None
    modalities: list[str] | None = None
    messages: list[dict[str, object]] | None = None


class ResponseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    input: object | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: object | None = None
    stream: bool | None = None


class AnthropicMessageRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    messages: list[dict[str, object]] | None = None
    system: object | None = None
    stream: bool | None = None


class SearchRequest(BaseModel):
    prompt: str = Field(..., min_length=1)


class EditableFileTaskRequest(BaseModel):
    prompt: str = ""
    base64_images: list[str] = Field(default_factory=list)
    client_task_id: str | None = None


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        raise


def _task_created_at(task: dict[str, object]) -> int:
    value = str(task.get("created_at") or "").strip()
    if value:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                from datetime import datetime

                return int(datetime.strptime(value[:26], fmt).timestamp())
            except ValueError:
                continue
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except Exception:
            pass
    return int(time.time())


def _image_task_status(status: object) -> str:
    value = str(status or "").strip().lower()
    if value == "success":
        return "succeeded"
    if value == "error":
        return "failed"
    if value in {"running", "queued"}:
        return value
    return "queued"


def _image_generation_task_response(task: dict[str, object]) -> dict[str, object]:
    status = _image_task_status(task.get("status"))
    response: dict[str, object] = {
        "id": str(task.get("id") or ""),
        "object": "image.generation",
        "status": status,
        "created": _task_created_at(task),
    }
    if task.get("model"):
        response["model"] = task.get("model")
    if task.get("progress"):
        response["progress"] = task.get("progress")
    if task.get("elapsed_secs") is not None:
        response["elapsed_secs"] = task.get("elapsed_secs")
    if task.get("conversation_id"):
        response["conversation_id"] = task.get("conversation_id")
    if status == "succeeded":
        response["data"] = task.get("data") if isinstance(task.get("data"), list) else []
        if isinstance(task.get("usage"), dict):
            response["usage"] = task.get("usage")
        if task.get("duration_ms") is not None:
            response["duration_ms"] = task.get("duration_ms")
    elif status == "failed":
        response["error"] = {
            "message": str(task.get("error") or "image generation failed"),
            "type": "image_generation_error",
            "code": "image_generation_failed",
        }
        if task.get("duration_ms") is not None:
            response["duration_ms"] = task.get("duration_ms")
    return response


def _background_task_id(value: str | None) -> str:
    task_id = str(value or "").strip()
    return task_id or f"imgtask_{uuid4().hex}"


def _is_background_task(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_models(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            return await run_in_threadpool(openai_v1_models.list_models)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.post("/v1/images/generations")
    async def generate_images(
            body: ImageGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        payload["base_url"] = resolve_image_base_url(request)
        call = LoggedCall(identity, "/v1/images/generations", body.model, "文生图", request_text=body.prompt)
        await filter_or_log(call, body.prompt)
        if _is_background_task(body.background):
            if body.stream:
                raise HTTPException(status_code=400, detail={"error": "background tasks do not support stream=true"})
            task = await run_in_threadpool(
                image_task_service.submit_generation,
                identity,
                client_task_id=_background_task_id(body.client_task_id),
                prompt=body.prompt,
                model=body.model,
                size=body.size,
                quality=body.quality,
                n=body.n,
                response_format=body.response_format,
                base_url=payload["base_url"],
            )
            call.log("后台任务已创建", {"task_id": task.get("id"), "status": task.get("status")})
            return _image_generation_task_response(task)
        return await call.run(openai_v1_image_generations.handle, payload)

    @router.get("/v1/images/generations/{task_id}")
    async def get_image_generation_task(task_id: str, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        result = await run_in_threadpool(image_task_service.list_tasks, identity, [task_id])
        items = result.get("items") if isinstance(result, dict) else []
        if not isinstance(items, list) or not items:
            raise HTTPException(status_code=404, detail={"error": "image generation task not found"})
        return _image_generation_task_response(items[0])

    @router.post("/v1/images/edits")
    async def edit_images(
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload, image_sources = await parse_image_edit_request(request)
        prompt = str(payload["prompt"])
        model = str(payload["model"])
        call = LoggedCall(identity, "/v1/images/edits", model, "图生图", request_text=prompt)
        await filter_or_log(call, prompt)
        images = await read_image_sources(image_sources)
        payload["images"] = images
        payload["base_url"] = resolve_image_base_url(request)
        if _is_background_task(payload.get("background")):
            if payload.get("stream"):
                raise HTTPException(status_code=400, detail={"error": "background tasks do not support stream=true"})
            task = await run_in_threadpool(
                image_task_service.submit_edit,
                identity,
                client_task_id=_background_task_id(payload.get("client_task_id")),
                prompt=prompt,
                model=model,
                size=payload["size"],
                quality=payload["quality"],
                base_url=payload["base_url"],
                images=images,
            )
            call.log("后台任务已创建", {"task_id": task.get("id"), "status": task.get("status")})
            return _image_generation_task_response(task)
        return await call.run(openai_v1_image_edit.handle, payload)

    @router.post("/v1/chat/completions")
    async def create_chat_completion(body: ChatCompletionRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("prompt"), payload.get("messages"))
        call = LoggedCall(
            identity,
            "/v1/chat/completions",
            model,
            "文本生成",
            request_text=request_preview,
            request_shape=request_shape(payload.get("messages")),
        )
        await filter_or_log(call, request_preview)
        return await call.run(openai_v1_chat_complete.handle, payload)

    @router.post("/v1/responses")
    async def create_response(body: ResponseCreateRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("input"), payload.get("instructions"))
        call = LoggedCall(
            identity,
            "/v1/responses",
            model,
            "Responses",
            request_text=request_preview,
            request_shape=request_shape(payload.get("input")),
        )
        await filter_or_log(call, request_preview)
        return await call.run(openai_v1_response.handle, payload)

    @router.post("/v1/messages")
    async def create_message(
            body: AnthropicMessageRequest,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="x-api-key"),
            anthropic_version: str | None = Header(default=None, alias="anthropic-version"),
    ):
        identity = require_identity(authorization or (f"Bearer {x_api_key}" if x_api_key else None))
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("system"), payload.get("messages"), payload.get("tools"))
        call = LoggedCall(identity, "/v1/messages", model, "Messages", request_text=request_preview)
        await filter_or_log(call, request_preview)
        return await call.run(anthropic_v1_messages.handle, payload, sse="anthropic")

    @router.post("/v1/search")
    async def search(body: SearchRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        call = LoggedCall(identity, "/v1/search", openai_search.MODEL, "搜索", request_text=body.prompt)
        await filter_or_log(call, body.prompt)
        return await call.run(openai_search.handle, body.model_dump(mode="python"))

    @router.get("/v1/editable-file-tasks")
    async def list_editable_file_tasks(ids: str = "", authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        task_ids = [item.strip() for item in ids.split(",") if item.strip()]
        return await run_in_threadpool(editable_file_task_service.list_tasks, identity, task_ids)

    @router.get("/files/{file_path:path}")
    async def download_editable_file(file_path: str):
        try:
            path = await run_in_threadpool(editable_file_task_service.public_file_path, file_path)
        except Exception as exc:
            raise HTTPException(status_code=404, detail={"error": "file not found"}) from exc
        return FileResponse(path, filename=path.name)

    @router.post("/v1/ppt/generations")
    async def create_ppt_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/v1/ppt/generations", "gpt-5-5-thinking", "PPT生成任务", request_text=body.prompt), body.prompt)
        return await run_in_threadpool(
            editable_file_task_service.submit_ppt,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    @router.post("/v1/psd/generations")
    async def create_psd_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/v1/psd/generations", "gpt-5-5-thinking", "PSD生成任务", request_text=body.prompt), body.prompt)
        return await run_in_threadpool(
            editable_file_task_service.submit_psd,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    return router
