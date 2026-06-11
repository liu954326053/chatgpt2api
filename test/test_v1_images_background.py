from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.ai as ai_module

AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}


class FakeImageTaskService:
    def __init__(self) -> None:
        self.generation_calls = []
        self.tasks = {
            "imgtask_done": {
                "id": "imgtask_done",
                "status": "success",
                "mode": "generate",
                "model": "gpt-image-2",
                "created_at": "2026-06-11 12:00:00",
                "updated_at": "2026-06-11 12:00:05",
                "data": [{"url": "http://testserver/images/fake.png"}],
                "usage": {"total_tokens": 123},
                "duration_ms": 5000,
            }
        }

    def submit_generation(self, identity, **kwargs):
        self.generation_calls.append((identity, kwargs))
        task_id = kwargs["client_task_id"]
        task = {
            "id": task_id,
            "status": "queued",
            "mode": "generate",
            "model": kwargs["model"],
            "created_at": "2026-06-11 12:00:00",
            "updated_at": "2026-06-11 12:00:00",
        }
        self.tasks[task_id] = task
        return task

    def list_tasks(self, _identity, ids):
        return {
            "items": [self.tasks[task_id] for task_id in ids if task_id in self.tasks],
            "missing_ids": [task_id for task_id in ids if task_id not in self.tasks],
        }


class V1ImagesBackgroundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake_tasks = FakeImageTaskService()
        self.task_patcher = mock.patch.object(ai_module, "image_task_service", self.fake_tasks)
        self.task_patcher.start()
        self.addCleanup(self.task_patcher.stop)
        self.handler_calls = []
        self.handler_patcher = mock.patch.object(
            ai_module.openai_v1_image_generations,
            "handle",
            side_effect=self._sync_handler,
        )
        self.handler_patcher.start()
        self.addCleanup(self.handler_patcher.stop)
        app = FastAPI()
        app.include_router(ai_module.create_router())
        self.client = TestClient(app)

    def _sync_handler(self, payload):
        self.handler_calls.append(payload)
        return {"created": 123, "data": [{"b64_json": "abc"}]}

    def test_generation_without_background_keeps_sync_behavior(self) -> None:
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={"prompt": "cat", "model": "gpt-image-2"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], [{"b64_json": "abc"}])
        self.assertEqual(len(self.handler_calls), 1)
        self.assertEqual(self.fake_tasks.generation_calls, [])

    def test_generation_non_boolean_background_keeps_sync_behavior(self) -> None:
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={"prompt": "cat", "model": "gpt-image-2", "background": "transparent"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], [{"b64_json": "abc"}])
        self.assertEqual(len(self.handler_calls), 1)
        self.assertEqual(self.fake_tasks.generation_calls, [])

    def test_generation_background_returns_task_id(self) -> None:
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={
                "prompt": "cat",
                "model": "gpt-image-2",
                "background": True,
                "client_task_id": "imgtask_custom",
                "response_format": "url",
                "n": 2,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["id"], "imgtask_custom")
        self.assertEqual(payload["object"], "image.generation")
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(len(self.fake_tasks.generation_calls), 1)
        call = self.fake_tasks.generation_calls[0][1]
        self.assertEqual(call["client_task_id"], "imgtask_custom")
        self.assertEqual(call["prompt"], "cat")
        self.assertEqual(call["quality"], "auto")
        self.assertEqual(call["n"], 2)
        self.assertEqual(call["response_format"], "url")
        self.assertEqual(self.handler_calls, [])

    def test_generation_background_rejects_stream(self) -> None:
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={"prompt": "cat", "background": True, "stream": True},
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("background tasks do not support stream", response.text)

    def test_get_generation_task_returns_completed_result(self) -> None:
        response = self.client.get("/v1/images/generations/imgtask_done", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["id"], "imgtask_done")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["data"], [{"url": "http://testserver/images/fake.png"}])
        self.assertEqual(payload["usage"], {"total_tokens": 123})

    def test_get_generation_task_404(self) -> None:
        response = self.client.get("/v1/images/generations/missing", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
