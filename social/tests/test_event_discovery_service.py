import unittest
import os
from unittest.mock import Mock, patch

from services import event_discovery_service as discovery
from services.skill_loader import load_skill

REAL_ACTIVE_EVENT_INVENTORY = discovery._active_event_inventory


class EventDiscoveryServiceTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {
            "EVENT_ADAPTIVE_SUPPLEMENTAL_SEARCH": "off",
            "EVENT_URL_HEALTHCHECK_ENABLED": "off",
        })
        self._env.start()
        self._inventory = patch.object(
            discovery, "_active_event_inventory",
            side_effect=lambda categories: {
                "status": "unavailable",
                "category_counts": {category: 0 for category in categories},
            },
        )
        self._inventory_mock = self._inventory.start()
        self._reconcile = patch.object(
            discovery, "_reconcile_event_inventory",
            return_value={
                "status": "success", "deduplicated_count": 0,
                "pruned_count": 0, "category_counts": {},
            },
        )
        self._reconcile_mock = self._reconcile.start()

    def tearDown(self):
        self._reconcile.stop()
        self._inventory.stop()
        self._env.stop()

    def test_all_discovery_skills_are_versioned_and_cover_supported_categories(self):
        self.assertTrue(set(discovery.SUPPORTED_CATEGORIES).issubset(discovery.CATEGORY_SPECS))
        for category, spec in discovery.CATEGORY_SPECS.items():
            with self.subTest(category=category):
                skill = load_skill(spec["skill"])
                self.assertEqual(skill["version"], "1")
                self.assertIn("Kaohsiung", skill["instructions"])
                self.assertTrue(spec["official_query"])
                self.assertTrue(spec["broad_query"])
                self.assertTrue(spec["extraction_focus"])
                self.assertTrue(spec["recovery_terms"])

    def test_queries_use_month_terms_instead_of_exact_date_range(self):
        now = discovery.datetime(2026, 8, 7, tzinfo=discovery.TAIPEI)

        queries = discovery._queries("高雄", now, 30)

        self.assertTrue(queries)
        self.assertTrue(all("2026年8月" in query for _category, query in queries))
        self.assertTrue(all("2026年9月" in query for _category, query in queries))
        self.assertTrue(all(" 到 " not in query for _category, query in queries))

    @patch.object(discovery.time, "sleep")
    @patch.object(discovery.requests, "post")
    def test_event_ingest_retries_transient_timeout_then_succeeds(self, post, sleep):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "success", "events": [], "ingested_count": 0,
        }
        post.side_effect = [discovery.requests.Timeout(), response]

        with patch.dict(os.environ, {"EVENT_INGEST_MAX_ATTEMPTS": "2"}):
            result = discovery._post_event_ingest(
                {"search_results": []}, timeout_seconds=180, category="市集",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2.0)

    @patch.object(discovery.time, "sleep")
    @patch.object(discovery.requests, "post")
    def test_event_ingest_preserves_agent_error_code_after_retries(self, post, sleep):
        response = Mock()
        response.status_code = 503
        response.json.return_value = {
            "detail": {"error_code": "JSONDecodeError"},
        }
        error = discovery.requests.HTTPError(response=response)
        response.raise_for_status.side_effect = error
        post.return_value = response

        with patch.dict(os.environ, {"EVENT_INGEST_MAX_ATTEMPTS": "2"}):
            with self.assertRaises(discovery._EventIngestError) as captured:
                discovery._post_event_ingest(
                    {"search_results": []}, timeout_seconds=180, category="音樂",
                )

        self.assertEqual(captured.exception.code, "jsondecodeerror")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_obviously_stale_metadata_is_rejected_before_extraction(self):
        now = discovery.datetime(2026, 8, 7, tzinfo=discovery.TAIPEI)

        self.assertEqual(
            discovery._metadata_rejection_reason(
                {"title": "2025 高雄夏日市集", "snippet": "活動資訊"}, now,
            ),
            "stale_year",
        )
        self.assertEqual(
            discovery._metadata_rejection_reason(
                {"title": "高雄夏日市集", "snippet": "2026 最新活動"}, now,
            ),
            "",
        )

    def test_similar_source_titles_are_sent_in_the_same_bounded_batch(self):
        batches = discovery._source_batches([
            {"title": "落日飛車 Q來Q去 亞洲巡迴 高雄站", "source_url": "https://a.example/event"},
            {"title": "2026 落日飛車《Q來Q去》亞洲巡迴 高雄站", "source_url": "https://b.example/event"},
            {"title": "港邊夏日市集", "source_url": "https://c.example/event"},
        ])

        self.assertEqual(len(batches[0]), 2)

    def test_source_title_normalization_folds_styled_unicode(self):
        self.assertEqual(
            discovery._normalized_source_title("𝐓𝐚𝐥𝐤 𝐁𝐁𝐐, Taste Life"),
            discovery._normalized_source_title("Talk BBQ, Taste Life"),
        )

    def test_music_batches_are_smaller_to_bound_extraction_latency(self):
        items = [
            {"title": f"演出 {index}", "source_url": f"https://example.com/{index}"}
            for index in range(7)
        ]
        batches = discovery._source_batches(items, "音樂")
        self.assertTrue(batches)
        self.assertTrue(all(len(batch) <= 3 for batch in batches))
        self.assertTrue(all(
            len(batch) <= discovery.MAX_INGEST_SOURCES_PER_BATCH for batch in batches
        ))

    def test_extraction_cache_key_changes_with_page_content(self):
        first = discovery._extraction_cache_key("市集", [{
            "title": "市集", "snippet": "8月活動", "source_url": "https://example.com/a",
            "skill_name": "event-market-discovery", "skill_version": "1",
        }])
        second = discovery._extraction_cache_key("市集", [{
            "title": "市集", "snippet": "9月活動", "source_url": "https://example.com/a",
            "skill_name": "event-market-discovery", "skill_version": "1",
        }])
        self.assertNotEqual(first, second)

    @patch.object(discovery, "_extraction_cache")
    def test_cache_requires_all_events_to_still_be_active(self, collection):
        collection.find_one.return_value = {"payload": {"events": [
            {"event_id": "event-a", "title": "A"},
        ]}}
        with patch.dict(os.environ, {"AYUE_TEST_MODE": "off"}):
            miss = discovery._cached_extraction("市集", [], set())
            hit = discovery._cached_extraction("市集", [], {"event-a"})
        self.assertIsNone(miss)
        self.assertIsNotNone(hit)

    def test_source_healthcheck_falls_back_to_bounded_get_when_head_is_blocked(self):
        head_response = Mock(status_code=405)
        get_response = Mock(status_code=200)

        with patch.object(discovery.requests, "head", return_value=head_response), patch.object(
            discovery.requests, "get", return_value=get_response,
        ) as get:
            alive = discovery._source_url_alive("https://example.com/event")

        self.assertTrue(alive)
        self.assertEqual(get.call_args.kwargs["headers"]["Range"], "bytes=0-2047")
        self.assertTrue(get.call_args.kwargs["stream"])

    @patch.object(discovery, "extract_web", return_value=({"pages": []}, None))
    @patch.object(discovery, "search_web")
    def test_every_category_search_result_carries_its_owned_skill(self, search_web, _extract_web):
        call_number = 0

        def result_for_query(_query, **_kwargs):
            nonlocal call_number
            call_number += 1
            return ({"results": [{
                "title": f"Event {call_number}",
                "url": f"https://example.com/event-{call_number}",
                "snippet": "2026/08/20 高雄",
            }]}, None)

        search_web.side_effect = result_for_query
        results, errors = discovery._bounded_search_results("高雄", 30)

        self.assertEqual(errors, [])
        self.assertGreaterEqual(search_web.call_count, len(discovery.SUPPORTED_CATEGORIES))
        self.assertLessEqual(search_web.call_count, len(discovery.SUPPORTED_CATEGORIES) * 2)
        for item in results:
            category = item["discovery_category"]
            self.assertEqual(item["skill_name"], discovery.CATEGORY_SPECS[category]["skill"])
            self.assertEqual(item["skill_version"], "1")
        self.assertEqual({item["discovery_category"] for item in results}, set(discovery.SUPPORTED_CATEGORIES))
        self.assertTrue(all(
            sum(item["discovery_category"] == category for item in results)
            <= discovery.MAX_RESULTS_PER_CATEGORY
            for category in discovery.SUPPORTED_CATEGORIES
        ))

    @patch.object(discovery, "extract_web", return_value=({"pages": []}, None))
    @patch.object(discovery, "search_web")
    def test_official_results_do_not_starve_each_category_fallback_query(self, search_web, _extract_web):
        search_web.return_value = ({"results": [
            {
                "title": f"Candidate {index}",
                "url": f"https://example.com/{search_web.call_count}-{index}",
                "snippet": "高雄活動",
            }
            for index in range(5)
        ]}, None)

        results, _errors = discovery._bounded_search_results("高雄", 30, ("音樂",))

        self.assertEqual(search_web.call_count, 2)
        self.assertLessEqual(len(results), discovery.MAX_RESULTS_PER_CATEGORY)

    @patch.object(discovery, "extract_web", return_value=({"pages": []}, None))
    @patch.object(discovery, "search_web")
    def test_page_extraction_is_fair_and_category_specific(self, search_web, extract_web):
        call_number = 0

        def three_results(_query, **_kwargs):
            nonlocal call_number
            call_number += 1
            return ({"results": [
                {
                    "title": f"Event {call_number}-{index}",
                    "url": f"https://example.com/{call_number}-{index}",
                    "snippet": "2026/08/20 高雄",
                }
                for index in range(3)
            ]}, None)

        search_web.side_effect = three_results
        discovery._bounded_search_results("高雄", 30)

        extracted_url_count = sum(len(call.args[0]) for call in extract_web.call_args_list)
        self.assertEqual(extracted_url_count, discovery.MAX_EXTRACT_RESULTS)
        self.assertLessEqual(
            extract_web.call_count, discovery.MAX_EXTRACT_RESULTS,
        )
        extraction_queries = [call.kwargs["query"] for call in extract_web.call_args_list]
        for category in discovery.SUPPORTED_CATEGORIES:
            self.assertTrue(any(
                discovery.CATEGORY_SPECS[category]["extraction_focus"] in query
                for query in extraction_queries
            ))

    @patch.object(discovery, "extract_web", return_value=({"pages": []}, None))
    @patch.object(discovery, "search_web")
    def test_search_results_are_deduplicated_and_bounded(self, search_web, _extract_web):
        search_web.return_value = ({"results": [
            {"title": "活動 A", "url": "https://example.com/a", "snippet": "高雄 8/10"},
            {"title": "重複 A", "url": "https://example.com/a", "snippet": "重複"},
        ]}, None)
        results, errors = discovery._bounded_search_results("高雄", 30)
        keys = [(item["discovery_category"], item["source_url"]) for item in results]
        for category in discovery.SUPPORTED_CATEGORIES:
            self.assertLessEqual(keys.count((category, "https://example.com/a")), 1)
        urls = [item["source_url"] for item in results]
        self.assertIn(discovery.KAOHSIUNG_MARKET_SOURCE, urls)
        self.assertEqual(errors, [])

    @patch.object(discovery, "extract_web", return_value=({"pages": []}, None))
    @patch.object(discovery, "search_web", return_value=({"results": []}, None))
    def test_kaohsiung_curated_feeds_cover_every_category(self, _search_web, _extract_web):
        results, errors = discovery._bounded_search_results("高雄", 30)

        self.assertEqual(errors, [])
        for category in discovery.SUPPORTED_CATEGORIES:
            with self.subTest(category=category):
                sources = discovery.REGION_CURATED_SOURCES["高雄"][category]
                self.assertTrue(sources)
                selected_urls = {
                    item["source_url"] for item in results
                    if item["discovery_category"] == category
                }
                self.assertTrue(
                    {
                        source["source_url"].format(year=discovery.datetime.now(discovery.TAIPEI).year)
                        for source in sources
                    }.issubset(selected_urls)
                )

    @patch.object(discovery, "extract_web", return_value=({"pages": []}, None))
    @patch.object(discovery, "search_web", return_value=({"results": []}, None))
    def test_other_regions_do_not_inherit_kaohsiung_curated_feeds(
        self, _search_web, _extract_web,
    ):
        results, _errors = discovery._bounded_search_results("台南", 30, ("市集",))

        self.assertEqual(results, [])

    @patch.object(discovery, "extract_web", return_value=({"pages": []}, None))
    @patch.object(discovery, "search_web", return_value=({"results": []}, None))
    def test_market_only_run_does_not_search_other_categories(self, search_web, _extract_web):
        results, _errors = discovery._bounded_search_results("高雄", 30, ("市集",))
        self.assertTrue(results)
        self.assertTrue(all(item["discovery_category"] == "市集" for item in results))
        self.assertTrue(all("市集" in call.args[0] for call in search_web.call_args_list))

    @patch.object(discovery, "extract_web")
    @patch.object(discovery, "search_web")
    def test_top_sources_are_enriched_with_bounded_page_content(self, search_web, extract_web):
        search_web.return_value = ({"results": [
            {"title": "活動 A", "url": "https://example.com/a", "snippet": "活動摘要"},
        ]}, None)
        extract_web.return_value = ({"pages": [{
            "url": "https://example.com/a", "content": "2026/08/20 09:00 高雄港",
        }]}, None)
        results, errors = discovery._bounded_search_results("高雄", 30)
        selected = next(item for item in results if item["source_url"] == "https://example.com/a")
        self.assertIn("2026/08/20 09:00 高雄港", selected["snippet"])
        self.assertLessEqual(len(selected["snippet"]), 1500)
        self.assertEqual(errors, [])

    @patch.object(discovery, "extract_web")
    @patch.object(discovery, "search_web")
    def test_supplemental_search_uses_failure_hint_and_skips_known_urls(
        self, search_web, extract_web,
    ):
        search_web.side_effect = [
            ({"results": [
                {"title": "Known", "url": "https://example.com/known", "snippet": "old"},
                {"title": "New A", "url": "https://example.com/new-a", "snippet": "new"},
                {"title": "New B", "url": "https://example.com/new-b", "snippet": "new"},
            ]}, None),
            ({"results": [
                {"title": "New C", "url": "https://example.com/new-c", "snippet": "new"},
                {"title": "New D", "url": "https://example.com/new-d", "snippet": "new"},
                {"title": "New E", "url": "https://example.com/new-e", "snippet": "new"},
            ]}, None),
            ({"results": [
                {"title": "New F", "url": "https://example.com/new-f", "snippet": "new"},
            ]}, None),
        ]
        extract_web.return_value = ({"pages": [{
            "url": "https://example.com/new-a", "content": "2026/08/20 高雄場地",
        }]}, None)

        results, errors = discovery._supplemental_search_results(
            "高雄", 30, "運動",
            excluded_urls={"https://example.com/known"},
            validation_counts={"missing_or_invalid_date": 2},
            time_gaps=("late",),
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        self.assertLessEqual(len(results), discovery.MAX_SUPPLEMENTAL_RESULTS_PER_CATEGORY)
        self.assertEqual(search_web.call_count, 3)
        self.assertNotIn("https://example.com/known", [item["source_url"] for item in results])
        self.assertIn("完整活動日期", search_web.call_args_list[0].args[0])
        self.assertIn("本月下旬", search_web.call_args_list[2].args[0])
        self.assertIn("2026/08/20 高雄場地", results[0]["snippet"])

    @patch.object(discovery, "request_event_opportunity_scan")
    @patch.object(discovery, "project_event_relevance", return_value={"status": "success"})
    @patch.object(discovery, "_supplemental_search_results")
    @patch.object(discovery, "_bounded_search_results")
    @patch.object(discovery.requests, "post")
    def test_adaptive_search_only_retries_underfilled_category(
        self, post, bounded, supplemental, _relevance, _request_scan,
    ):
        bounded.return_value = ([
            {"title": "M", "snippet": "M", "source_url": "https://example.com/m", "discovery_category": "市集"},
            {"title": "S", "snippet": "S", "source_url": "https://example.com/s", "discovery_category": "運動"},
        ], [])
        supplemental.return_value = ([{
            "title": "S2", "snippet": "S2", "source_url": "https://example.com/s2",
            "discovery_category": "運動", "skill_name": "event-sports-discovery",
            "skill_version": "1",
        }], [])

        def response(events, validation=None):
            value = Mock()
            value.raise_for_status.return_value = None
            value.json.return_value = {
                "status": "success", "ingested_count": len(events), "events": events,
                "validation_counts": validation or {},
            }
            return value

        post.side_effect = [
            response([
                {"dedupe_key": "m1", "title": "M1", "category": "市集"},
                {"dedupe_key": "m2", "title": "M2", "category": "市集"},
            ]),
            response([], {"model_returned_empty": 1}),
            response([{"dedupe_key": "s1", "title": "S1", "category": "運動"}]),
        ]

        with patch.dict(os.environ, {
            "EVENT_ADAPTIVE_SUPPLEMENTAL_SEARCH": "on",
            "EVENT_DISCOVERY_TARGET_PER_CATEGORY": "2",
        }):
            result = discovery.discover_and_ingest_events(
                region="高雄", window_days=30, categories=["市集", "運動"],
            )

        supplemental.assert_called_once()
        self.assertEqual(supplemental.call_args.args[2], "運動")
        self.assertEqual(result["category_counts"], {"市集": 2, "運動": 1})
        self.assertEqual(result["supplemental"]["triggered_categories"], ["運動"])
        self.assertEqual(result["supplemental"]["additional_ingested_count"], 1)
        self.assertEqual(post.call_args_list[-1].kwargs["timeout"], (3, 180))

    def test_active_inventory_counts_graph_supply_by_category(self):
        now = discovery.datetime.now(discovery.TAIPEI).timestamp()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "success",
            "user_count": 11,
            "events": [
                {"category": "市集", "starts_at": now + 2 * 86400},
                {"category": "市集", "starts_at": now + 12 * 86400},
                {"category": "音樂", "starts_at": now + 25 * 86400},
                {"category": "其他", "starts_at": now + 86400},
            ],
        }
        with patch.object(discovery.requests, "get", return_value=response):
            result = REAL_ACTIVE_EVENT_INVENTORY(("市集", "音樂", "運動"))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["user_count"], 11)
        self.assertEqual(result["category_counts"], {"市集": 2, "音樂": 1, "運動": 0})
        self.assertEqual(result["time_bucket_counts"]["市集"], {
            "near": 1, "middle": 1, "late": 0,
        })
        self.assertEqual(result["time_gaps"]["市集"], ["late"])
        self.assertEqual(result["time_gaps"]["音樂"], ["near", "middle"])
        self.assertEqual(result["temporal_status"], "available")

    @patch.object(discovery, "project_event_relevance", return_value={"status": "empty"})
    @patch.object(discovery, "_supplemental_search_results", return_value=([], []))
    @patch.object(discovery, "_bounded_search_results")
    @patch.object(discovery.requests, "post")
    def test_adaptive_search_caps_recovery_categories(
        self, post, bounded, supplemental, _relevance,
    ):
        categories = list(discovery.SUPPORTED_CATEGORIES[:4])
        bounded.return_value = ([
            {
                "title": category, "snippet": category,
                "source_url": f"https://example.com/{index}",
                "discovery_category": category,
            }
            for index, category in enumerate(categories)
        ], [])
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "success", "ingested_count": 0, "events": [],
            "validation_counts": {"model_returned_empty": 1},
        }
        post.return_value = response

        with patch.dict(os.environ, {
            "EVENT_ADAPTIVE_SUPPLEMENTAL_SEARCH": "on",
            "EVENT_ADAPTIVE_SUPPLEMENTAL_MAX_CATEGORIES": "2",
        }):
            result = discovery.discover_and_ingest_events(
                region="高雄", window_days=30, categories=categories,
            )

        self.assertEqual(supplemental.call_count, 2)
        self.assertEqual(result["supplemental"]["max_categories"], 2)
        self.assertEqual(result["supplemental"]["triggered_categories"], categories[:2])

    @patch.object(discovery, "project_event_relevance", return_value={"status": "empty"})
    @patch.object(discovery, "_supplemental_search_results")
    @patch.object(discovery, "_bounded_search_results")
    @patch.object(discovery.requests, "post")
    def test_graph_inventory_at_target_skips_supplemental_search(
        self, post, bounded, supplemental, _relevance,
    ):
        bounded.return_value = ([{
            "title": "Market", "snippet": "Market",
            "source_url": "https://example.com/market", "discovery_category": "市集",
        }], [])
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "success", "ingested_count": 0, "events": [],
            "validation_counts": {"model_returned_empty": 1},
        }
        post.return_value = response
        inventory = {
            "status": "success", "category_counts": {"市集": 4},
            "user_count": 11,
        }
        self._inventory_mock.side_effect = [
            inventory, inventory, inventory,
        ]

        with patch.dict(os.environ, {
            "EVENT_ADAPTIVE_SUPPLEMENTAL_SEARCH": "on",
            "EVENT_DISCOVERY_TARGET_PER_CATEGORY": "4",
        }):
            result = discovery.discover_and_ingest_events(
                region="高雄", window_days=30, categories=["市集"],
            )

        supplemental.assert_not_called()
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertEqual(result["active_category_counts"], {"市集": 4})

    @patch.object(discovery, "project_event_relevance", return_value={"status": "empty"})
    @patch.object(discovery, "_supplemental_search_results", return_value=([], []))
    @patch.object(discovery, "_bounded_search_results")
    @patch.object(discovery.requests, "post")
    def test_graph_inventory_at_target_does_not_overfill_for_a_time_gap(
        self, post, bounded, supplemental, _relevance,
    ):
        bounded.return_value = ([{
            "title": "Market", "snippet": "Market",
            "source_url": "https://example.com/market", "discovery_category": "市集",
        }], [])
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "success", "ingested_count": 0, "events": [],
            "validation_counts": {},
        }
        post.return_value = response
        inventory = {
            "status": "success",
            "user_count": 11,
            "category_counts": {"市集": 4},
            "time_bucket_counts": {"市集": {"near": 2, "middle": 2, "late": 0}},
            "time_gaps": {"市集": ["late"]},
            "temporal_status": "available",
        }
        self._inventory_mock.side_effect = [inventory, inventory, inventory]

        with patch.dict(os.environ, {
            "EVENT_ADAPTIVE_SUPPLEMENTAL_SEARCH": "on",
            "EVENT_DISCOVERY_TARGET_PER_CATEGORY": "4",
        }):
            result = discovery.discover_and_ingest_events(
                region="高雄", window_days=30, categories=["市集"],
            )

        supplemental.assert_not_called()
        self.assertEqual(result["coverage"]["status"], "complete")

    @patch.object(discovery, "requests")
    @patch.object(discovery, "request_event_opportunity_scan")
    @patch.object(discovery, "project_event_relevance")
    @patch.object(discovery, "_bounded_search_results")
    def test_ingest_receives_only_bounded_projection(
        self, bounded, relevance_projection, request_scan, requests_module,
    ):
        bounded.return_value = ([{
            "title": "活動 A", "snippet": "高雄 8/10", "source_url": "https://example.com/a",
            "discovery_category": "市集",
            "skill_name": "event-market-discovery", "skill_version": "1",
        }], [])
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "success", "ingested_count": 1,
            "events": [{
                "dedupe_key": "internal", "event_id": "internal",
                "title": "活動 A", "summary": "摘要", "venue": "高雄港",
                "starts_at": 1_800_000_000, "ends_at": 1_800_003_600,
                "source_url": "https://example.com/a", "tags": ["戶外"], "vibes": ["熱鬧"],
            }],
        }
        requests_module.post.return_value = response
        relevance_projection.return_value = {
            "status": "success", "event_count": 1, "relevance_count": 2,
            "avoidance_count": 1,
        }

        result = discovery.discover_and_ingest_events(region="高雄", window_days=30)

        sent = requests_module.post.call_args.kwargs["json"]
        self.assertEqual(sent["region"], "高雄")
        self.assertEqual(sent["window_days"], 30)
        self.assertEqual(
            set(sent["search_results"][0]),
            {
                "title", "snippet", "source_url", "discovery_category",
                "skill_name", "skill_version",
            },
        )
        self.assertEqual(result["ingested_count"], 1)
        self.assertNotIn("dedupe_key", result["events"][0])
        self.assertNotIn("event_id", result["events"][0])
        self.assertEqual(result["relevance"]["link_count"], 3)
        relevance_projection.assert_called_once()
        request_scan.assert_called_once_with()

    @patch.object(discovery, "project_event_relevance", return_value={"status": "queued"})
    @patch.object(discovery, "_bounded_search_results")
    @patch.object(discovery.requests, "post")
    def test_categories_are_ingested_sequentially_with_long_timeout(
        self, post, bounded, _relevance_projection,
    ):
        first, second = discovery.SUPPORTED_CATEGORIES[:2]
        bounded.return_value = ([
            {"title": "A", "snippet": "A", "source_url": "https://example.com/a", "discovery_category": first},
            {"title": "B", "snippet": "B", "source_url": "https://example.com/b", "discovery_category": second},
        ], [])
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "success", "ingested_count": 0, "events": []}
        post.return_value = response

        discovery.discover_and_ingest_events(
            region="高雄", window_days=30, categories=[first, second],
        )

        self.assertEqual(post.call_count, 2)
        sent_categories = [
            call.kwargs["json"]["search_results"][0]["discovery_category"]
            for call in post.call_args_list
        ]
        self.assertEqual(sent_categories, [first, second])
        self.assertTrue(all(call.kwargs["timeout"] == (3, 600) for call in post.call_args_list))

    @patch.object(discovery, "project_event_relevance", return_value={"status": "empty"})
    @patch.object(discovery, "_bounded_search_results")
    @patch.object(discovery.requests, "post")
    def test_long_category_source_set_is_ingested_in_one_bounded_batch(
        self, post, bounded, _relevance_projection,
    ):
        bounded.return_value = ([
            {
                "title": f"Food {index}", "snippet": "dated food event",
                "source_url": f"https://example.com/food-{index}",
                "discovery_category": "美食",
            }
            for index in range(4)
        ], [])
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "success", "ingested_count": 0, "events": []}
        post.return_value = response

        discovery.discover_and_ingest_events(
            region="高雄", window_days=30, categories=["美食"],
        )

        self.assertEqual(post.call_count, 2)
        self.assertTrue(all(
            len(call.kwargs["json"]["search_results"])
            <= discovery.MAX_INGEST_SOURCES_PER_BATCH
            for call in post.call_args_list
        ))
        self.assertEqual(post.call_args.kwargs["json"]["max_events"], 6)

    @patch.object(discovery, "requests")
    @patch.object(discovery, "_bounded_search_results", return_value=([], ["web_timeout"]))
    def test_search_failure_never_calls_agent_ingest(self, _bounded, requests_module):
        result = discovery.discover_and_ingest_events(region="高雄", window_days=30)
        self.assertEqual(result["status"], "search_failed")
        requests_module.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
