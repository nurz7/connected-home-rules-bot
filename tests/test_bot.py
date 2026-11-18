import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from telegram import Update

from bot import API_ERROR_TEXT, DEFAULT_HR_URL, HISTORY_LENGTH, build_application
from rules_ai import FALLBACK, RulesAPIError, RulesAssistant

ROOT = Path(__file__).resolve().parents[1]

VACATION = {"clauses": ["3.1"], "missing_details": []}
VACATION_DEADLINE = {"clauses": ["3.2"], "missing_details": []}
NO_RULE = {"clauses": [], "missing_details": ["предоставляется ли сотрудникам ДМС"]}
NO_CONTEXT = {"clauses": [], "missing_details": []}
DMS_TEXT = "В правилах не указано, предоставляется ли сотрудникам ДМС. Обратитесь в HR."


def gemini_response(selection: dict) -> httpx.Response:
    return httpx.Response(200, json={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": json.dumps(selection)}]},
        }],
    })


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assistant = RulesAssistant(ROOT / "rules.txt", "example-key")

    async def test_answer_only_uses_source_and_handles_partial_question(self):
        def gemini(request):
            payload = json.loads(request.content)
            self.assertEqual(request.headers["x-goog-api-key"], "example-key")
            self.assertNotIn("example-key", str(request.url))
            self.assertIn("[3.1] Ежегодный оплачиваемый отпуск — 24 календарных дня.",
                          payload["systemInstruction"]["parts"][0]["text"])
            return gemini_response({
                "clauses": ["3.1", "3.1"],
                "missing_details": ["предоставляется ли сотрудникам ДМС"],
                "answer": "Пятьдесят дней",
            })

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(gemini))
        with patch("rules_ai.httpx.AsyncClient", return_value=mock_client):
            answer = await self.assistant.answer("Какой отпуск и есть ли ДМС?")
        self.assertEqual(
            answer.text,
            "Ежегодный оплачиваемый отпуск — 24 календарных дня.\n"
            f"(Пункт 3 правил)\n\n{DMS_TEXT}",
        )
        self.assertTrue(answer.missing)

    def test_full_answer_is_short_and_uses_the_document(self):
        answer = self.assistant._format_answer(VACATION)
        self.assertEqual(answer.text,
                         "Ежегодный оплачиваемый отпуск — 24 календарных дня.\n(Пункт 3 правил)")
        self.assertFalse(answer.missing)

    def test_unclear_question_uses_exact_fallback(self):
        answer = self.assistant._format_answer(NO_CONTEXT)
        self.assertEqual(answer.text, FALLBACK)
        self.assertTrue(answer.missing)

    def test_unknown_policy_names_the_unanswered_question(self):
        answer = self.assistant._format_answer(NO_RULE)
        self.assertEqual(answer.text, DMS_TEXT)
        self.assertNotIn("Пункт", answer.text)
        self.assertTrue(answer.missing)

    def test_dress_code_has_a_complete_subject(self):
        answer = self.assistant._format_answer({"clauses": ["10.1"], "missing_details": []})
        self.assertEqual(answer.text, "Дресс-код: свободный, но опрятный.\n(Пункт 10 правил)")
        self.assertFalse(answer.missing)

    def test_shorts_get_general_context_and_a_specific_limit(self):
        answer = self.assistant._format_answer({
            "clauses": ["10.1"],
            "missing_details": ["разрешено ли носить шорты в офисе"],
        })
        self.assertEqual(
            answer.text,
            "Дресс-код: свободный, но опрятный.\n(Пункт 10 правил)\n\n"
            "В правилах не указано, разрешено ли носить шорты в офисе. Обратитесь в HR.",
        )
        self.assertTrue(answer.missing)

    def test_each_unanswered_part_is_named_without_duplicate_details(self):
        answer = self.assistant._format_answer({
            "clauses": [],
            "missing_details": [
                "предоставляется ли сотрудникам ДМС", "оплачивает ли компания посещение спортзала",
                "предоставляется ли сотрудникам ДМС.",
            ],
        })
        self.assertEqual(answer.text,
                         "В правилах не указано, предоставляется ли сотрудникам ДМС.\n"
                         "В правилах не указано, оплачивает ли компания посещение спортзала. Обратитесь в HR.")
        self.assertTrue(answer.missing)

    def test_invalid_selections_are_errors_rather_than_missing_rules(self):
        for selection in [
            None, [], {}, {"clauses": []},
            {"clauses": ["3.1"], "missing_details": "ДМС"},
            {"clauses": ["99.1"], "missing_details": []},
            {"clauses": [3], "missing_details": []},
            {"clauses": [], "missing_details": [3]},
            {"clauses": [], "missing_details": [" "]},
            {"clauses": [], "missing_details": ["..."]},
            {"clauses": [], "missing_details": ["есть ли ДМС\nновая строка"]},
            {"clauses": [], "missing_details": ["a" * 251]},
            {"clauses": [], "missing_details": ["есть ли ДМС"] * 11},
        ]:
            with self.subTest(selection=selection), self.assertRaises(RulesAPIError):
                self.assistant._format_answer(selection)

    async def test_broken_model_response_is_not_missing_policy(self):
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: gemini_response({"clauses": ["99.1"], "missing_details": []})
        ))
        with patch("rules_ai.httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(RulesAPIError):
                await self.assistant.answer("Вопрос")

    async def test_false_missing_details_are_reviewed_against_full_document(self):
        cases = [
            (
                "Во сколько закрывается офис по пятницам?",
                {"clauses": [], "missing_details": ["во сколько закрывается офис по пятницам"]},
                ["6.3"], "Офис открыт с 8:00 до 20:00.",
            ),
            (
                "Можно сегодня подать заявку на удалёнку на сегодня?",
                {"clauses": ["2.2"], "missing_details": ["разрешено ли подавать заявку на удаленку в день ее начала"]},
                ["2.2"], "Заявку нужно подать минимум за 1 день.",
            ),
            (
                "Можно взять отпуск на неделю?",
                {"clauses": ["3.1", "3.3"], "missing_details": ["разрешено ли делить отпуск на части меньше 14 дней"]},
                ["3.3"], "одна из частей должна быть не меньше 14 дней",
            ),
            (
                "Компания оплачивает 150 000 тенге за каждый курс?",
                {"clauses": ["9.1"], "missing_details": ["оплачивает ли компания обучение суммой 150 000 тенге за каждый курс"]},
                ["9.1"], "до 150 000 ₸ в год на сотрудника",
            ),
        ]
        for question, draft, corrected_ids, expected in cases:
            requests = []

            def gemini(request):
                payload = json.loads(request.content)
                data = json.loads(payload["contents"][0]["parts"][0]["text"])
                requests.append(data)
                self.assertEqual(data["question"], question)
                self.assertEqual(data["history"], [])
                instruction = payload["systemInstruction"]["parts"][0]["text"]
                self.assertIn("[6.3] Офис открыт с 8:00 до 20:00.", instruction)
                if "draft" in data:
                    self.assertEqual(data["draft"], draft)
                    self.assertIn("ПРОВЕРКА ПРЕДВАРИТЕЛЬНОГО ОТВЕТА", instruction)
                    return gemini_response({"clauses": corrected_ids, "missing_details": []})
                return gemini_response(draft)

            client = httpx.AsyncClient(transport=httpx.MockTransport(gemini))
            with self.subTest(question=question), patch("rules_ai.httpx.AsyncClient", return_value=client):
                answer = await self.assistant.answer(question)
                self.assertIn(expected, answer.text)
                self.assertNotIn("HR", answer.text)
                self.assertNotIn("не указано", answer.text)
                self.assertFalse(answer.missing)
                self.assertEqual(len(requests), 2)

    async def test_full_answer_only_needs_one_request(self):
        requests = []

        def gemini(request):
            requests.append(request)
            return gemini_response(VACATION)

        client = httpx.AsyncClient(transport=httpx.MockTransport(gemini))
        with patch("rules_ai.httpx.AsyncClient", return_value=client):
            answer = await self.assistant.answer("Сколько дней отпуска?")
        self.assertFalse(answer.missing)
        self.assertEqual(len(requests), 1)

    async def test_review_preserves_real_unknowns(self):
        for selection in [
            NO_RULE,
            {"clauses": ["10.1"], "missing_details": ["разрешено ли носить шорты в офисе"]},
        ]:
            requests = []

            def gemini(request):
                payload = json.loads(request.content)
                requests.append(json.loads(payload["contents"][0]["parts"][0]["text"]))
                return gemini_response(selection)

            client = httpx.AsyncClient(transport=httpx.MockTransport(gemini))
            with self.subTest(selection=selection), patch("rules_ai.httpx.AsyncClient", return_value=client):
                answer = await self.assistant.answer("Вопрос сотрудника")
                self.assertTrue(answer.missing)
                self.assertIn(selection["missing_details"][0], answer.text)
                self.assertIn("Обратитесь в HR", answer.text)
                self.assertEqual(len(requests), 2)

    async def test_review_failure_is_a_service_error(self):
        for result in [429, {"clauses": ["99.1"], "missing_details": []}]:
            def gemini(request):
                payload = json.loads(request.content)
                data = json.loads(payload["contents"][0]["parts"][0]["text"])
                if "draft" not in data:
                    return gemini_response(NO_RULE)
                return httpx.Response(result) if isinstance(result, int) else gemini_response(result)

            client = httpx.AsyncClient(transport=httpx.MockTransport(gemini))
            with self.subTest(result=result), patch("rules_ai.httpx.AsyncClient", return_value=client):
                with self.assertRaises(RulesAPIError):
                    await self.assistant.answer("Есть ДМС?")

    async def test_review_removes_false_gap_and_preserves_real_gap(self):
        draft = {"clauses": ["2.2"], "missing_details": [
            "можно ли подать заявку на удалёнку в день начала", "предоставляется ли сотрудникам ДМС",
        ]}
        corrected = {"clauses": ["2.2"], "missing_details": ["предоставляется ли сотрудникам ДМС"]}

        def gemini(request):
            payload = json.loads(request.content)
            data = json.loads(payload["contents"][0]["parts"][0]["text"])
            return gemini_response(corrected if "draft" in data else draft)

        client = httpx.AsyncClient(transport=httpx.MockTransport(gemini))
        with patch("rules_ai.httpx.AsyncClient", return_value=client):
            answer = await self.assistant.answer("Можно подать заявку на удалёнку на сегодня и есть ли ДМС?")
        self.assertIn("минимум за 1 день", answer.text)
        self.assertIn(DMS_TEXT, answer.text)
        self.assertNotIn("не указано, можно ли подать", answer.text)
        self.assertTrue(answer.missing)

    async def test_telegram_replies_history_log_and_service_errors(self):
        responses = iter([
            VACATION, VACATION_DEADLINE, NO_RULE, 429,
            {"clauses": [], "missing_details": ["какие премии выплачивает компания"]},
            NO_CONTEXT, NO_CONTEXT,
            VACATION, VACATION, VACATION, VACATION, VACATION,
        ])
        received = []
        sent = []

        def gemini(request):
            payload = json.loads(request.content)
            data = json.loads(payload["contents"][0]["parts"][0]["text"])
            if "draft" in data:
                return gemini_response(data["draft"])
            received.append(data)
            result = next(responses)
            return httpx.Response(result) if isinstance(result, int) else gemini_response(result)

        async def telegram(url, method, request_data, **kwargs):
            if url.endswith("/getMe"):
                result = {"id": 123, "is_bot": True, "first_name": "Rules", "username": "rules_bot"}
            elif url.endswith("/sendMessage"):
                params = request_data.parameters
                sent.append(params)
                result = {
                    "message_id": len(sent), "date": 1760000000,
                    "chat": {"id": params["chat_id"], "type": "private"},
                    "text": params["text"],
                }
            else:
                self.fail("Unexpected Telegram method")
            return 200, json.dumps({"ok": True, "result": result}).encode()

        real_client_class = httpx.AsyncClient
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"NO_PROXY": "*", "no_proxy": "*"}):
            log_path = Path(tmp) / "unanswered.jsonl"
            app = build_application(
                "123:example", self.assistant, DEFAULT_HR_URL, log_path
            )

            async def send(update_id, chat_id, text):
                message = {
                    "chat": {"id": chat_id, "type": "private"},
                    "message_id": update_id,
                    "date": 1760000000,
                    "from": {"id": chat_id, "is_bot": False, "first_name": "Demo"},
                    "text": text,
                }
                if text.startswith("/"):
                    message["entities"] = [
                        {"type": "bot_command", "offset": 0, "length": len(text)}
                    ]
                await app.process_update(Update.de_json(
                    {"update_id": update_id, "message": message}, app.bot
                ))

            with (
                patch("telegram.request.HTTPXRequest.do_request", side_effect=telegram),
                patch("rules_ai.httpx.AsyncClient", side_effect=lambda **kwargs:
                      real_client_class(transport=httpx.MockTransport(gemini), **kwargs)),
            ):
                async with app:
                    await send(1, 101, "/start")
                    await send(2, 101, "Сколько дней отпуск?")
                    await send(3, 101, "А за сколько подать?")
                    await send(4, 101, "Есть ДМС?")
                    with self.assertLogs("bot", level="WARNING"):
                        await send(5, 101, "Когда аванс?")
                    await send(6, 102, "Какие премии?")
                    await send(7, 101, "/reset")
                    await send(8, 101, "А за сколько подать?")
                    await send(9, 101, "/hr")
                    await send(10, 101, "/start")
                    await send(11, 101, "А за сколько подать?")
                    for update_id in range(12, 17):
                        await send(update_id, 101, "Сколько дней отпуск?")

            self.assertIn("тестовый бот", sent[0]["text"])
            self.assertIn(DEFAULT_HR_URL, str(sent[0]["reply_markup"]))
            self.assertIn("24 календарных дня", sent[1]["text"])
            self.assertIn("за 14 дней", sent[2]["text"])
            self.assertEqual(sent[3]["text"], DMS_TEXT)
            self.assertIn(DEFAULT_HR_URL, str(sent[3]["reply_markup"]))
            self.assertEqual(sent[4]["text"], API_ERROR_TEXT)
            self.assertEqual(received[1]["history"][0]["question"], "Сколько дней отпуск?")
            self.assertEqual(received[4]["history"], [])
            self.assertEqual(received[5]["history"], [])
            self.assertEqual(received[6]["history"], [])
            self.assertIn(DEFAULT_HR_URL, str(sent[8]["reply_markup"]))
            self.assertIn("Контакты Connected Home", sent[8]["text"])
            self.assertEqual(len(received[-1]["history"]), HISTORY_LENGTH)
            self.assertEqual(len(app.chat_data[101]["history"]), HISTORY_LENGTH)
            self.assertEqual(len(received[3]["history"]), HISTORY_LENGTH)
            self.assertEqual(len(sent), 16)
            self.assertEqual(
                [json.loads(row)["question"] for row in log_path.read_text(encoding="utf-8").splitlines()],
                ["Есть ДМС?", "Какие премии?", "А за сколько подать?", "А за сколько подать?"],
            )

    async def test_network_timeout_is_a_service_error(self):
        def timeout(request):
            raise httpx.ReadTimeout("Timeout", request=request)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        with patch("rules_ai.httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(RulesAPIError):
                await self.assistant.answer("Сколько дней отпуска?")

    async def test_blocked_and_malformed_model_outputs_are_service_errors(self):
        for body in [
            {"candidates": []},
            {"candidates": [{"finishReason": "SAFETY"}]},
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "not JSON"}]}}]},
        ]:
            mock_client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body)
            ))
            with self.subTest(body=body), patch("rules_ai.httpx.AsyncClient", return_value=mock_client):
                with self.assertRaises(RulesAPIError):
                    await self.assistant.answer("Вопрос")
