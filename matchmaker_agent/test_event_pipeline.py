import os
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


os.environ.setdefault("NEO4J_URI", "bolt://stub.invalid:7687")
os.environ.setdefault("NEO4J_USERNAME", "stub")
os.environ.setdefault("NEO4J_PASSWORD", "stub")
os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:9")
os.environ.setdefault("OLLAMA_API_KEY", "stub")

from matchmaker import EVENT_HOOK_LLM_TIMEOUT_SECONDS, MatchmakerAgent


class EventPipelineTests(unittest.TestCase):
    def _agent_with_failed_llm(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        agent.client = MagicMock()
        agent.client.with_options.return_value = agent.client
        agent.client.chat.completions.create.side_effect = RuntimeError("offline")
        agent.model = "stub"
        return agent

    def test_expired_event_cleanup_returns_deleted_ids_for_port_8000(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        agent._graph_config = MagicMock(
            return_value=("bolt://stub", ("stub", "stub"), "neo4j"),
        )
        driver = MagicMock()
        session = MagicMock()
        driver.__enter__.return_value = driver
        driver.session.return_value.__enter__.return_value = session
        constraint_result = MagicMock()
        intent_result = MagicMock()
        delete_result = MagicMock()
        delete_result.single.return_value = {"deleted": 2}
        session.run.side_effect = [
            constraint_result,
            intent_result,
            [{"event_id": "event_1"}, {"event_id": "event_2"}],
            delete_result,
        ]
        with patch("matchmaker.GraphDatabase.driver", return_value=driver):
            result = agent.clean_expired_events(include_ids=True)
        self.assertEqual(result["deleted_count"], 2)
        self.assertEqual(result["event_ids"], ["event_1", "event_2"])

    def test_invitation_order_fallback_asks_less_connected_party_first(self):
        agent = self._agent_with_failed_llm()
        result = agent.choose_event_invitation_order({
            "target_links": ["戶外"],
            "candidate_links": ["戶外", "音樂"],
        })
        self.assertEqual(result["first"], "target")
        agent.client.with_options.assert_not_called()

    def test_invitation_order_fallback_can_ask_candidate_first(self):
        agent = self._agent_with_failed_llm()
        result = agent.choose_event_invitation_order({
            "target_links": ["戶外", "運動"],
            "candidate_links": ["戶外"],
        })
        self.assertEqual(result["first"], "candidate")

    def test_event_source_url_rejects_private_hosts(self):
        self.assertFalse(MatchmakerAgent._safe_event_source_url("http://127.0.0.1/event"))
        self.assertFalse(MatchmakerAgent._safe_event_source_url("http://localhost/event"))
        self.assertTrue(MatchmakerAgent._safe_event_source_url("https://example.com/event"))

    def test_iso_event_date_uses_taipei_timezone_without_inventing_minutes(self):
        timestamp = MatchmakerAgent._event_timestamp("2026-08-29T00:00:00+08:00")
        self.assertEqual(timestamp, 1787932800)

    def test_event_dedupe_distinguishes_events_on_same_listing_page(self):
        first = MatchmakerAgent._event_dedupe_key(
            "數位學習論壇", 1785945600,
        )
        second = MatchmakerAgent._event_dedupe_key(
            "國際酒展", 1786032000,
        )
        self.assertNotEqual(first, second)

    def test_event_dedupe_distinguishes_same_title_at_different_venues(self):
        first = MatchmakerAgent._event_dedupe_key("巡迴演出", 1785945600, "場地 A")
        second = MatchmakerAgent._event_dedupe_key("巡迴演出", 1785945600, "場地 B")
        self.assertNotEqual(first, second)

    def test_event_dedupe_does_not_merge_different_date_and_venue_by_source_url(self):
        first = MatchmakerAgent._event_dedupe_key(
            "轉角幸福市集", 1783699200, "場地 A",
            "https://kcginfo.kcg.gov.tw/pda/active.aspx",
        )
        second = MatchmakerAgent._event_dedupe_key(
            "轉角幸福市集", 1784304000, "場地 B",
            "https://kcginfo.kcg.gov.tw/pda/active.aspx#schedule",
        )
        self.assertNotEqual(first, second)

    def test_event_dedupe_keeps_different_titles_from_listing_page(self):
        first = MatchmakerAgent._event_dedupe_key(
            "轉角幸福市集", 1783699200, "場地 A",
            "https://kcginfo.kcg.gov.tw/pda/active.aspx",
        )
        second = MatchmakerAgent._event_dedupe_key(
            "港邊音樂祭", 1783699200, "場地 A",
            "https://kcginfo.kcg.gov.tw/pda/active.aspx",
        )
        self.assertNotEqual(first, second)

    def test_event_dedupe_does_not_use_tracking_url_as_identity(self):
        first = MatchmakerAgent._event_dedupe_key(
            "轉角幸福市集", 1783699200, "場地 A",
            "https://example.com/events/market?utm_source=search",
        )
        second = MatchmakerAgent._event_dedupe_key(
            "轉角幸福市集", 1783699200, "場地 A",
            "https://example.com/events/market?ref=homepage",
        )
        self.assertEqual(first, second)

    def test_event_identity_normalization_folds_styled_unicode(self):
        plain = MatchmakerAgent._event_dedupe_key(
            "Talk BBQ, Taste Life｜臺味炙場", 1785945600, "高雄港", category="美食",
        )
        styled = MatchmakerAgent._event_dedupe_key(
            "𝐓𝐚𝐥𝐤 𝐁𝐁𝐐, 𝐓𝐚𝐬𝐭𝐞 𝐋𝐢𝐟𝐞｜臺味炙場",
            1785945600, "高雄港", "https://another.example/event", category="美食",
        )
        self.assertEqual(plain, styled)

    def test_similar_same_day_same_venue_events_merge_without_name_rules(self):
        base = {
            "category": "運動", "starts_at": 1785945600,
            "venue": "高雄國家體育場",
        }
        first = {**base, "title": "高雄5000公尺挑戰賽（第三場）"}
        second = {**base, "title": "高雄 - 5000公尺挑戰賽 (8/21)"}
        self.assertTrue(MatchmakerAgent._events_are_same_identity(first, second))

    def test_same_day_same_title_different_venue_does_not_merge(self):
        base = {
            "category": "市集", "starts_at": 1785945600,
            "title": "週末生活市集",
        }
        self.assertFalse(MatchmakerAgent._events_are_same_identity(
            {**base, "venue": "駁二藝術特區"},
            {**base, "venue": "衛武營戶外廣場"},
        ))

    def test_inventory_reconcile_deduplicates_and_enforces_category_cap(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        now = int(time.time())
        rows = []
        for index in range(8):
            title = f"活動 {index}"
            if index == 1:
                title = "𝐀𝐜𝐭𝐢𝐯𝐢𝐭𝐲 Zero"
            elif index == 0:
                title = "Activity Zero"
            rows.append({
                "element_id": f"node-{index}",
                "properties": {
                    "id": f"event-{index}", "dedupe_key": f"key-{index}",
                    "title": title, "venue": "高雄港", "category": "運動",
                    "starts_at": now + (86400 if index < 2 else index * 86400),
                    "expires_at": now + 40 * 86400, "source_tier": "curated",
                    "source_url": f"https://example.com/{index}",
                },
                "tags": ["戶外"], "vibes": ["熱鬧"],
            })

        driver = MagicMock()
        session = MagicMock()
        driver.__enter__.return_value = driver
        driver.session.return_value.__enter__.return_value = session

        def run_query(query, **kwargs):
            if "properties(event) AS properties" in query:
                return rows
            if "RETURN event.category AS category" in query:
                return [{"category": "運動", "count": 6}]
            return MagicMock()

        session.run.side_effect = run_query
        agent._graph_config = MagicMock(
            return_value=("bolt://stub", ("stub", "stub"), "neo4j"),
        )
        with patch("matchmaker.GraphDatabase.driver", return_value=driver):
            result = agent.reconcile_event_inventory(["運動"], max_per_category=6)

        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual(result["pruned_count"], 1)
        self.assertEqual(result["category_counts"], {"運動": 6})
        delete_call = next(
            call for call in session.run.call_args_list
            if "DETACH DELETE event" in call.args[0]
        )
        self.assertEqual(len(delete_call.kwargs["element_ids"]), 2)

    def test_same_source_identity_retains_venues_and_sessions(self):
        key = "stable-key"
        base = {
            "dedupe_key": key, "summary": "short", "expires_at": 300,
            "venue": "場地 A", "venues": ["場地 A"], "tags": ["市集"],
            "vibes": ["戶外"], "starts_at": 100, "ends_at": 150,
            "session_starts": [100], "session_ends": [150],
            "session_precisions": ["datetime"], "session_count": 1,
            "time_precision": "datetime", "category": "市集",
        }
        other = {
            **base, "summary": "a more complete summary", "venue": "場地 B",
            "venues": ["場地 B"], "tags": ["手作"], "vibes": ["文青"],
            "starts_at": 200, "ends_at": 250, "session_starts": [200],
            "session_ends": [250], "expires_at": 400,
        }
        result = MatchmakerAgent._merge_same_identity_events([base, other])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["venues"], ["場地 A", "場地 B"])
        self.assertEqual(result[0]["session_starts"], [100, 200])
        self.assertEqual(result[0]["ends_at"], 250)
        self.assertEqual(result[0]["expires_at"], 400)
        self.assertEqual(result[0]["summary"], "a more complete summary")

    def test_adjacent_identical_concert_dates_become_one_multi_session_event(self):
        base = {
            "normalized_title": "落日飛車q來q去亞洲巡迴高雄站",
            "normalized_venue": "高雄流行音樂中心",
            "title": "落日飛車《Q來Q去》亞洲巡迴 高雄站",
            "venue": "高雄流行音樂中心",
            "source_url": "https://example.com/concert",
            "category": "音樂", "summary": "concert", "time_precision": "datetime",
            "session_precisions": ["datetime"], "expires_at": 200000,
        }
        first = {**base, "starts_at": 100000, "ends_at": 103600,
                 "session_starts": [100000], "session_ends": [103600], "session_count": 1}
        second = {**base, "starts_at": 186400, "ends_at": 190000,
                  "session_starts": [186400], "session_ends": [190000], "session_count": 1,
                  "expires_at": 280000}

        merged = MatchmakerAgent._merge_multi_session_events([first, second])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["session_starts"], [100000, 186400])
        self.assertEqual(merged[0]["session_count"], 2)
        self.assertEqual(merged[0]["ends_at"], 190000)

    def test_markets_are_merged_across_sessions(self):
        base = {
            "normalized_title": "駁二週末市集", "normalized_venue": "駁二藝術特區",
            "title": "駁二週末市集", "venue": "駁二藝術特區",
            "source_url": "https://example.com/markets", "category": "市集",
            "summary": "market", "time_precision": "date",
            "session_precisions": ["date"], "expires_at": 300000,
        }
        events = [
            {**base, "starts_at": 100000, "ends_at": 103600,
             "session_starts": [100000], "session_ends": [103600], "session_count": 1},
            {**base, "starts_at": 186400, "ends_at": 190000,
             "session_starts": [186400], "session_ends": [190000], "session_count": 1},
        ]

        merged = MatchmakerAgent._merge_multi_session_events(events)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["session_count"], 2)

    def test_same_title_at_different_venues_is_never_merged(self):
        base = {
            "normalized_title": "巡迴演出", "title": "巡迴演出",
            "source_url": "https://example.com/tour", "category": "音樂",
            "summary": "tour", "time_precision": "datetime",
            "session_precisions": ["datetime"], "expires_at": 300000,
            "session_count": 1,
        }
        events = [
            {**base, "normalized_venue": "場地a", "venue": "場地 A",
             "starts_at": 100000, "ends_at": 103600,
             "session_starts": [100000], "session_ends": [103600]},
            {**base, "normalized_venue": "場地b", "venue": "場地 B",
             "starts_at": 186400, "ends_at": 190000,
             "session_starts": [186400], "session_ends": [190000]},
        ]

        self.assertEqual(len(MatchmakerAgent._merge_multi_session_events(events)), 2)

    def test_event_without_verified_date_is_rejected_before_graph_write(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        agent.client = MagicMock()
        agent.model = "stub"
        agent.event_model = "stub"
        message = MagicMock()
        message.content = json.dumps([{
            "title": "日期不明活動", "summary": "摘要", "venue": "高雄港",
            "starts_at": None, "ends_at": None,
            "source_url": "https://example.com/event", "region": "高雄",
            "tags": ["戶外"], "vibes": ["熱鬧"],
        }], ensure_ascii=False)
        agent.client.chat.completions.create.return_value.choices = [MagicMock(message=message)]

        result = agent.extract_and_ingest_search_results([{
            "title": "日期不明活動", "snippet": "沒有日期", "source_url": "https://example.com/event",
        }], region="高雄", window_days=30)

        self.assertEqual(result["ingested_count"], 0)

    def test_event_outside_thirty_day_window_is_rejected(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        agent.client = MagicMock()
        agent.model = "stub"
        agent.event_model = "stub"
        message = MagicMock()
        message.content = json.dumps([{
            "title": "太晚的活動", "summary": "摘要", "venue": "高雄港",
            "starts_at": int(time.time()) + 31 * 86400,
            "ends_at": int(time.time()) + 31 * 86400 + 3600,
            "source_url": "https://example.com/late", "region": "高雄",
            "tags": ["戶外"], "vibes": ["熱鬧"],
        }], ensure_ascii=False)
        agent.client.chat.completions.create.return_value.choices = [MagicMock(message=message)]

        result = agent.extract_and_ingest_search_results([{
            "title": "太晚的活動", "snippet": "下個月以後", "source_url": "https://example.com/late",
        }], region="高雄", window_days=30)

        self.assertEqual(result["ingested_count"], 0)

    def test_late_model_result_is_discarded_before_graph_write(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        agent.client = MagicMock()
        agent.model = "stub"
        agent.event_model = "stub"
        now = int(time.time())
        start = now + 86400
        taipei = timezone(timedelta(hours=8))
        date_text = datetime.fromtimestamp(start, taipei).strftime("%Y-%m-%d")
        message = MagicMock()
        message.content = json.dumps([{
            "title": "期限測試活動", "summary": "摘要", "venue": "高雄港",
            "starts_at": start, "ends_at": start + 3600,
            "time_precision": "datetime", "date_evidence": date_text,
            "source_url": "https://example.com/deadline", "region": "高雄",
            "tags": ["戶外"], "vibes": ["輕鬆"],
        }], ensure_ascii=False)
        agent.client.chat.completions.create.return_value.choices = [MagicMock(message=message)]

        with patch("matchmaker.GraphDatabase.driver") as graph:
            result = agent.extract_and_ingest_search_results([{
                "title": "期限測試活動", "snippet": f"活動日期 {date_text}",
                "source_url": "https://example.com/deadline",
                "discovery_category": "運動",
            }], region="高雄", window_days=30, write_deadline=time.time() - 1)

        self.assertEqual(result["ingested_count"], 0)
        self.assertEqual(result["validation_counts"]["write_deadline_expired"], 1)
        graph.assert_not_called()

    def test_event_dates_must_match_an_exact_source_evidence_quote(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        now = int(time.time())
        start = now + 86400
        wrong_end = now + 8 * 86400
        source_end = now + 2 * 86400
        taipei = timezone(timedelta(hours=8))
        start_text = datetime.fromtimestamp(start, taipei).strftime("%Y-%m-%d")
        source_end_text = datetime.fromtimestamp(source_end, taipei).strftime("%Y-%m-%d")
        evidence = f"活動日期 {start_text} 至 {source_end_text}"
        sessions = agent._validated_event_sessions({
            "starts_at": start,
            "ends_at": wrong_end,
            "time_precision": "date",
        }, now, now + 30 * 86400)

        self.assertTrue(agent._evidence_is_source_substring(evidence, f"活動名稱 {evidence}"))
        self.assertFalse(agent._event_sessions_match_date_evidence(sessions, evidence))

    def test_grounded_event_date_range_is_accepted(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        now = int(time.time())
        start = now + 86400
        end = now + 2 * 86400
        taipei = timezone(timedelta(hours=8))
        start_text = datetime.fromtimestamp(start, taipei).strftime("%Y-%m-%d")
        end_text = datetime.fromtimestamp(end, taipei).strftime("%Y-%m-%d")
        sessions = agent._validated_event_sessions({
            "starts_at": start,
            "ends_at": end,
            "time_precision": "date",
        }, now, now + 30 * 86400)

        self.assertTrue(agent._event_sessions_match_date_evidence(
            sessions, f"{start_text}～{end_text}",
        ))

    def test_ongoing_event_overlapping_window_is_accepted(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        now = int(time.time())

        sessions = agent._validated_event_sessions({
            "starts_at": now - 7 * 86400,
            "ends_at": now + 7 * 86400,
            "time_precision": "date",
        }, now, now + 30 * 86400)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["starts_at"], now - 7 * 86400)
        self.assertEqual(sessions[0]["ends_at"], now + 7 * 86400)

    def test_expired_event_is_rejected(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        now = int(time.time())

        sessions = agent._validated_event_sessions({
            "starts_at": now - 7 * 86400,
            "ends_at": now - 86400,
            "time_precision": "date",
        }, now, now + 30 * 86400)

        self.assertEqual(sessions, [])

    def test_ongoing_multi_day_event_falls_back_to_overall_interval(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        now = int(time.time())

        sessions = agent._validated_event_sessions({
            "starts_at": now - 12 * 3600,
            "ends_at": now + 2 * 86400,
            "time_precision": "datetime",
            "sessions": [{
                "starts_at": now - 12 * 3600,
                "ends_at": now - 3600,
                "time_precision": "datetime",
            }],
        }, now, now + 30 * 86400)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["ends_at"], now + 2 * 86400)

    def test_event_ingest_hot_path_never_deletes_existing_events(self):
        source = Path(__file__).with_name("matchmaker.py").read_text(encoding="utf-8")

        ingest_source = source.split("def extract_and_ingest_search_results", 1)[1]
        ingest_source = ingest_source.split("@staticmethod\n    def _load_event_skill", 1)[0]
        self.assertNotIn("DETACH DELETE legacy", ingest_source)
        self.assertNotIn("legacy_dedupe_key", ingest_source)
        self.assertNotIn("DETACH DELETE user", ingest_source)

    def test_empty_event_extraction_gets_one_strict_retry(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        agent.client = MagicMock()
        agent.model = "stub"
        agent.event_model = "stub"
        message = MagicMock()
        message.content = "[]"
        agent.client.chat.completions.create.return_value.choices = [MagicMock(message=message)]

        result = agent.extract_and_ingest_search_results([{
            "title": "待複檢活動", "snippet": "2026/08/20 高雄",
            "source_url": "https://example.com/event",
        }], region="高雄", window_days=30)

        self.assertEqual(agent.client.chat.completions.create.call_count, 2)
        self.assertEqual(result["validation_counts"], {"model_returned_empty": 1})

    def test_malformed_event_json_gets_one_strict_retry_instead_of_http_503(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        agent.client = MagicMock()
        agent.model = "stub"
        agent.event_model = "stub"
        malformed = MagicMock()
        malformed.content = "not-json"
        empty = MagicMock()
        empty.content = "[]"
        agent.client.chat.completions.create.side_effect = [
            MagicMock(choices=[MagicMock(message=malformed)]),
            MagicMock(choices=[MagicMock(message=empty)]),
        ]

        result = agent.extract_and_ingest_search_results([{
            "title": "待複檢活動", "snippet": "2026/08/20 高雄",
            "source_url": "https://example.com/event",
        }], region="高雄", window_days=30)

        self.assertEqual(agent.client.chat.completions.create.call_count, 2)
        self.assertEqual(result["validation_counts"], {"model_returned_empty": 1})

    def test_unavailable_event_model_falls_back_to_main_model(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        agent.client = MagicMock()
        agent.model = "fallback-model"
        agent.event_model = "subscription-model"
        denied = RuntimeError("subscription required")
        denied.status_code = 403
        message = MagicMock()
        message.content = "[]"
        fallback_response = MagicMock()
        fallback_response.choices = [MagicMock(message=message)]
        agent.client.chat.completions.create.side_effect = [
            denied, fallback_response, fallback_response,
        ]

        with patch.dict(os.environ, {"EVENT_EXTRACTION_FALLBACK_MODEL_ID": "fallback-model"}):
            result = agent.extract_and_ingest_search_results([{
                "title": "待複檢活動", "snippet": "2026/08/20 高雄",
                "source_url": "https://example.com/event",
            }], region="高雄", window_days=30)

        models = [call.kwargs["model"] for call in agent.client.chat.completions.create.call_args_list]
        self.assertEqual(models, ["subscription-model", "fallback-model", "fallback-model"])
        self.assertEqual(result["validation_counts"], {"model_returned_empty": 1})

    def test_discovery_category_is_server_owned_even_when_model_relabels_event(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        agent.client = MagicMock()
        agent.model = "stub"
        agent.event_model = "stub"
        starts_at = int(time.time()) + 86400
        taipei = timezone(timedelta(hours=8))
        starts_text = datetime.fromtimestamp(starts_at, taipei).strftime("%Y-%m-%d")
        ends_text = datetime.fromtimestamp(starts_at + 3600, taipei).strftime("%Y-%m-%d")
        evidence = f"活動日期 {starts_text} 至 {ends_text}"
        message = MagicMock()
        message.content = json.dumps([{
            "title": "高雄戶外球賽", "summary": "公開運動活動", "venue": "高雄體育場",
            "starts_at": starts_at, "ends_at": starts_at + 3600,
            "date_evidence": evidence,
            "category": "美食", "source_url": "https://example.com/sports",
            "region": "高雄", "tags": ["球賽"], "vibes": ["熱血"],
        }], ensure_ascii=False)
        agent.client.chat.completions.create.return_value.choices = [MagicMock(message=message)]
        driver = MagicMock()
        session = MagicMock()
        driver.__enter__.return_value = driver
        driver.session.return_value.__enter__.return_value = session
        expected_key = MatchmakerAgent._event_dedupe_key(
            "高雄戶外球賽", starts_at, "高雄體育場",
            "https://example.com/sports", category="運動",
        )

        def run_query(query, **_kwargs):
            if "RETURN event.dedupe_key AS dedupe_key" in query:
                return [{"dedupe_key": expected_key}]
            return MagicMock()

        session.run.side_effect = run_query
        agent._graph_config = MagicMock(return_value=("bolt://stub", ("stub", "stub"), "neo4j"))

        with patch("matchmaker.GraphDatabase.driver", return_value=driver):
            result = agent.extract_and_ingest_search_results([{
                "title": "高雄戶外球賽", "snippet": f"未來公開球賽 {evidence}",
                "source_url": "https://example.com/sports",
                "discovery_category": "運動",
                "skill_name": "event-sports-discovery", "skill_version": "1",
            }], region="高雄", window_days=30)

        self.assertEqual(result["ingested_count"], 1)
        self.assertEqual(result["events"][0]["category"], "運動")
        write_queries = [call.args[0] for call in session.run.call_args_list]
        self.assertFalse(any("DETACH DELETE" in query for query in write_queries))
        self.assertFalse(any("User" in query and "DELETE" in query for query in write_queries))

    def test_all_event_skill_loaders_are_allowlisted(self):
        expected_headings = {
            "event-exhibition-discovery": "Kaohsiung Exhibition Discovery",
            "event-market-discovery": "Kaohsiung Market Discovery",
            "event-music-discovery": "Kaohsiung Music Discovery",
            "event-sports-discovery": "Kaohsiung Sports Discovery",
            "event-festival-discovery": "Kaohsiung Festival Discovery",
            "event-food-discovery": "Kaohsiung Food Event Discovery",
        }
        for skill_name, heading in expected_headings.items():
            with self.subTest(skill_name=skill_name):
                self.assertIn(heading, MatchmakerAgent._load_event_skill(skill_name))
        self.assertEqual(MatchmakerAgent._load_event_skill("../../secret"), "")

    def test_event_match_query_uses_semantic_links_and_hard_avoidance(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        driver = MagicMock()
        session = MagicMock()
        driver.__enter__.return_value = driver
        driver.session.return_value.__enter__.return_value = session
        session.run.return_value = []
        agent._graph_config = MagicMock(return_value=("bolt://stub", ("stub", "stub"), "neo4j"))
        with unittest.mock.patch("matchmaker.GraphDatabase.driver", return_value=driver):
            result = agent.find_event_matches("owner")
        self.assertEqual(result, [])
        query = session.run.call_args.args[0]
        self.assertIn("EVENT_RELEVANCE", query)
        self.assertIn("EVENT_AVOIDANCE", query)
        self.assertIn("excluded_user_ids", query)
        self.assertNotIn("any(signal IN event_signals", query)

    def test_event_hook_fallback_never_exposes_candidate_identity(self):
        agent = self._agent_with_failed_llm()
        hook = agent.generate_proactive_event_hook("owner", {
            "event_name": "港邊生活市集",
            "candidate_id": "seed_user_08",
            "candidate_name": "小明",
            "target_links": ["戶外"],
            "candidate_links": ["市集"],
        })
        self.assertNotIn("seed_user_08", hook)
        self.assertNotIn("小明", hook)
        self.assertIn("港邊生活市集", hook)


    def test_event_hook_adds_event_name_when_model_omits_it(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        agent.client = MagicMock()
        agent.client.with_options.return_value = agent.client
        agent.model = "stub"
        message = MagicMock()
        message.content = "我想到一個也喜歡生活選物的人，要不要我幫你牽線？"
        agent.client.chat.completions.create.return_value.choices = [
            MagicMock(message=message),
        ]
        hook = agent.generate_proactive_event_hook("owner", {
            "event_name": "下一站，少女心市",
            "target_links": ["可愛風格"],
            "candidate_links": ["生活選物"],
        })
        agent.client.with_options.assert_called_once_with(
            timeout=EVENT_HOOK_LLM_TIMEOUT_SECONDS, max_retries=0,
        )
        self.assertIn("下一站，少女心市", hook)
        self.assertIn("生活選物", hook)

    def test_scoped_event_reset_never_deletes_users(self):
        from agent_api import reset_event_graph

        driver = MagicMock()
        session = MagicMock()
        driver.__enter__.return_value = driver
        driver.session.return_value.__enter__.return_value = session

        def run_query(query, **_kwargs):
            if "RETURN event.dedupe_key AS dedupe_key" in query:
                return [{"dedupe_key": expected_key}]
            return MagicMock()

        session.run.side_effect = run_query
        agent._graph_config = MagicMock(return_value=("bolt://stub", ("stub", "stub"), "neo4j"))

        with patch("matchmaker.GraphDatabase.driver", return_value=driver):
            result = agent.extract_and_ingest_search_results([{
                "title": "高雄戶外球賽", "snippet": f"未來公開球賽 {evidence}",
                "source_url": "https://example.com/sports",
                "discovery_category": "運動",
                "skill_name": "event-sports-discovery", "skill_version": "1",
            }], region="高雄", window_days=30)

        self.assertEqual(result["ingested_count"], 1)
        self.assertEqual(result["events"][0]["category"], "運動")
        write_queries = [call.args[0] for call in session.run.call_args_list]
        self.assertFalse(any("DETACH DELETE" in query for query in write_queries))
        self.assertFalse(any("User" in query and "DELETE" in query for query in write_queries))

    def test_all_event_skill_loaders_are_allowlisted(self):
        expected_headings = {
            "event-exhibition-discovery": "Kaohsiung Exhibition Discovery",
            "event-market-discovery": "Kaohsiung Market Discovery",
            "event-music-discovery": "Kaohsiung Music Discovery",
            "event-sports-discovery": "Kaohsiung Sports Discovery",
            "event-festival-discovery": "Kaohsiung Festival Discovery",
            "event-food-discovery": "Kaohsiung Food Event Discovery",
        }
        for skill_name, heading in expected_headings.items():
            with self.subTest(skill_name=skill_name):
                self.assertIn(heading, MatchmakerAgent._load_event_skill(skill_name))
        self.assertEqual(MatchmakerAgent._load_event_skill("../../secret"), "")

    def test_event_match_query_uses_semantic_links_and_hard_avoidance(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        driver = MagicMock()
        session = MagicMock()
        driver.__enter__.return_value = driver
        driver.session.return_value.__enter__.return_value = session
        session.run.return_value = []
        agent._graph_config = MagicMock(return_value=("bolt://stub", ("stub", "stub"), "neo4j"))
        with unittest.mock.patch("matchmaker.GraphDatabase.driver", return_value=driver):
            result = agent.find_event_matches("owner")
        self.assertEqual(result, [])
        query = session.run.call_args.args[0]
        self.assertIn("EVENT_RELEVANCE", query)
        self.assertIn("EVENT_AVOIDANCE", query)
        self.assertIn("excluded_user_ids", query)
        self.assertNotIn("any(signal IN event_signals", query)

    def test_event_hook_fallback_never_exposes_candidate_identity(self):
        agent = self._agent_with_failed_llm()
        hook = agent.generate_proactive_event_hook("owner", {
            "event_name": "港邊生活市集",
            "candidate_id": "seed_user_08",
            "candidate_name": "小明",
            "target_links": ["戶外"],
            "candidate_links": ["市集"],
        })
        self.assertNotIn("seed_user_08", hook)
        self.assertNotIn("小明", hook)
        self.assertIn("港邊生活市集", hook)


    def test_event_hook_adds_event_name_when_model_omits_it(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        agent.client = MagicMock()
        agent.client.with_options.return_value = agent.client
        agent.model = "stub"
        message = MagicMock()
        message.content = "我想到一個也喜歡生活選物的人，要不要我幫你牽線？"
        agent.client.chat.completions.create.return_value.choices = [
            MagicMock(message=message),
        ]
        hook = agent.generate_proactive_event_hook("owner", {
            "event_name": "下一站，少女心市",
            "target_links": ["可愛風格"],
            "candidate_links": ["生活選物"],
        })
        self.assertIn("下一站，少女心市", hook)
        self.assertIn("生活選物", hook)

    def test_scoped_event_reset_never_deletes_users(self):
        from agent_api import reset_event_graph

        driver = MagicMock()
        session = MagicMock()
        driver.__enter__.return_value = driver
        driver.session.return_value.__enter__.return_value = session

        def run_query(query, **_kwargs):
            result = MagicMock()
            if "RETURN count(event) AS count" in query:
                result.single.return_value = {"count": 12}
            elif "collect(DISTINCT event.id)" in query:
                result.single.return_value = {"event_ids": ["event-1"]}
            elif "collect(DISTINCT elementId(concept))" in query:
                result.single.return_value = {"concept_ids": ["concept-1", "concept-2"]}
            elif "RETURN count(concept) AS count" in query:
                result.single.return_value = {"count": 4}
            return result

        session.run.side_effect = run_query
        with patch("agent_api.GraphDatabase.driver", return_value=driver):
            result = reset_event_graph(confirm=True)

        self.assertEqual(result["events_deleted"], 12)
        self.assertEqual(result["orphan_concepts_deleted"], 4)
        self.assertEqual(result["event_ids"], ["event-1"])
        queries = [call.args[0] for call in session.run.call_args_list]
        self.assertTrue(any("MATCH (event:Event) DETACH DELETE event" in query for query in queries))
        self.assertTrue(any("elementId(concept) IN $concept_ids" in query for query in queries))
        self.assertFalse(any("User" in query and "DELETE" in query for query in queries))

    def test_known_kaohsiung_venue_aliases_match_region(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        now = int(time.time())
        sessions = [{
            "starts_at": now + 86400,
            "ends_at": now + 86400,
            "time_precision": "date",
        }]
        # venue 是駁二藝術特區，未直接含「高雄」字樣
        raw_event = {
            "title": "駁二當代藝術展",
            "venue": "駁二藝術特區大勇倉庫",
            "region": "駁二",
            "starts_at": now + 86400,
            "ends_at": now + 86400,
            "source_url": "https://example.com/pier2",
            "date_evidence": datetime.fromtimestamp(now + 86400, timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        }
        known_landmarks = (
            "駁二", "衛武營", "高流", "高雄流行音樂中心", "巨蛋", "高美館", "高雄市立美術館",
        )
        venue = raw_event["venue"]
        title = raw_event["title"]
        self.assertTrue(any(kv in venue for kv in known_landmarks) or any(kv in title for kv in known_landmarks))

    def test_loose_evidence_substring_matching_tolerates_whitespace_and_punctuation(self):
        agent = MatchmakerAgent.__new__(MatchmakerAgent)
        evidence = "2026/08/20 - 2026/08/25"
        source = "【活動資訊】展期：2026/08/20 ～ 2026/08/25（週一休館）"
        self.assertTrue(agent._evidence_is_source_substring(evidence, source))


if __name__ == "__main__":
    unittest.main()
