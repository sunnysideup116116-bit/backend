import os
import json
import time
from openai import OpenAI
from pathlib import Path
from dotenv import load_dotenv

project_env = Path(__file__).resolve().parents[1] / ".env"
local_env = Path(__file__).resolve().parent / ".env"
if project_env.exists():
    load_dotenv(dotenv_path=project_env)
if local_env.exists():
    load_dotenv(dotenv_path=local_env, override=True)

class MatchmakerAgent:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL")
        )
        self.model = os.getenv("LLM_MODEL_ID")
        self.system_prompt = """你叫阿月，是一位熟悉台灣校園生活的 AI 媒人。
你的語氣像熟朋友：溫暖、直率、有觀察力，可以小吐槽但不要刻薄或施壓。
任務：從 target_user 與 candidates 中只選出 1 位最值得牽線的人，輸出嚴格 JSON，不要 Markdown。

必須輸出：
{
  "matches": [
    {
      "matched_user_id": "候選人的 user_id",
      "contrast_label": "4-6 字性格風格，例如：沉穩自律型",
      "recommendation_reason": "阿月對發起者說的單段推薦文，120字內。必須稱發起者為你、候選人為他/她。",
      "receiver_reason": "阿月對接收者說的單段推薦文，100字內。必須稱接收者為你、發起者為他/她。",
      "distinctive_tags": ["4 個精煉短句，不加任何分類前綴"],
      "score_breakdown": {"context":0,"graph":0,"values":0,"personality":0,"conversation":0,"total":0},
      "top_reasons": ["兩個以資料為根據的理由"]
    }
  ]
}

資料語義：
- target_user 是發起者本人；每個 candidate 是候選人本人。不要互換 current_context、big_five、deep_profile、graph_memory。
- graph_memory 是該 user 自己的偏好與地雷；候選人的 graph_memory 只代表候選人，不代表發起者。
- deep_profile 是價值觀、依附/關係需求、壓力因應、未來想像，權重高於 Big Five。

決策規則：
1. 先看「此刻情境是否對題」。target_user 明確提出的活動/場景是最高優先 evidence。
2. 近期情境評分 context 佔 30%；雙方 graph_memory 佔 25%；deep_profile/價值觀 20%；Big Five 15%；立即可聊話題 10%。total 必須是加權總分。
3. 如果任何一方的 DISLIKES_TRAIT 明確命中對方特質，原則上不得推薦。
4. 如果候選人沒有與目標活動直接相關的 current_context、initial_interest 或明確可聊連結，context 要大幅降分。
5. 三位候選人都不完全對題時，可選「最能接住此刻狀態」的人；用自然的橋樑寫法說明同能量、同場景、可互補或可聊點，不要每次用「老實說，這輪沒有人剛好...」這種制式開頭。
6. 只有活動落差很大、可能讓使用者誤會時，才簡短提醒不是同活動；提醒只能一筆帶過，重點要放在為什麼仍值得介紹。
7. 禁止把弱連結寫成強連結；「都在晚上」「都想出門」「都重視及時行樂」只能算弱連結，不可單獨當強推薦理由。
8. 禁止使用「靈魂雙胞胎」「靈魂契合度高到不行」「天生一對」等過度肯定說法。
9. 若 target_user 明確說「找不一樣的人」，意思是換風格/換上一位，不代表可以忽略當下活動需求；仍須能接住 current_context。
10. 即使候選人都不完美，也必須選出 1 位最能接住當下需求的人；但理由要誠實，不可把弱連結包裝成強契合。
11. matches 陣列必須剛好 1 個元素，不可輸出第二位備選。
12. recommendation_reason 最多 120 字；receiver_reason 最多 100 字；不要寫長篇分析。
13. top_reasons 只能放 2 個資料中明確存在的理由，不可補故事。
14. distinctive_tags 必須綜合候選人的 current_context、initial_interest、big_five.summary、deep_profile，萃取 4 個最特殊、強烈、主導性、或可能成為拒絕理由的具體特徵/狀態；只能輸出短句，不可加「鮮明特質：」「近期情境：」「興趣：」等前綴。

發起者 Graph Memory：
[GRAPH_MEMORY_PLACEHOLDER]

全域法則：
[GLOBAL_HEURISTICS_PLACEHOLDER]

發起者 Deep Profile：
[DEEP_PROFILE_PLACEHOLDER]
"""
    def match(self, target_user, candidates, graph_memory="", global_heuristics="", target_deep_profile=None):
        total_start = time.perf_counter()
        payload = {
            "target_user": target_user,
            "candidates": candidates,
            "graph_memory": graph_memory
        }
        
        if target_deep_profile:
            payload["target_deep_profile"] = target_deep_profile
        
        memory_text = graph_memory if graph_memory else "目前圖庫中尚無該使用者的偏好或地雷紀錄。"
        system_content = self.system_prompt.replace("[GRAPH_MEMORY_PLACEHOLDER]", memory_text)
        
        heuristics_text = global_heuristics if global_heuristics else "目前沒有可用的全域法則。"
        system_content = system_content.replace("[GLOBAL_HEURISTICS_PLACEHOLDER]", heuristics_text)
        
        if target_deep_profile:
            deep_profile_text = json.dumps(target_deep_profile, ensure_ascii=False, indent=2)
        else:
            deep_profile_text = "目前沒有 deep_profile，請改用 Big Five、current_context 與 graph_memory 判斷。"
        system_content = system_content.replace("[DEEP_PROFILE_PLACEHOLDER]", deep_profile_text)
        
        print("🧠 MatchmakerAgent 正在評估候選人...")
        try:
            payload_text = json.dumps(payload, ensure_ascii=False)
            print(
                "[TIMING][MatchmakerAgent.match] before LLM "
                f"system_chars={len(system_content)} payload_chars={len(payload_text)} "
                f"candidates={len(candidates)}"
            )
            step_start = time.perf_counter()
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": payload_text}
                ],
                temperature=0.7,
            )
            content = response.choices[0].message.content
            print(
                "[TIMING][MatchmakerAgent.match] LLM call: "
                f"{time.perf_counter() - step_start:.3f}s output_chars={len(content) if content else 0}"
            )
            print(f"[TIMING][MatchmakerAgent.match] total: {time.perf_counter() - total_start:.3f}s")
            return content
        except Exception as e:
            print(f"[TIMING][MatchmakerAgent.match] failed after {time.perf_counter() - total_start:.3f}s")
            return json.dumps({"matches": [], "error": f"matchmaker_failed: {e}"}, ensure_ascii=False)

    def generate_graph_reflection(self, history_text, explicit_reasons=None):
        print(f"🧠 [Debug] generate_graph_reflection explicit_reasons={explicit_reasons}")
        explicit_section = ""
        if explicit_reasons:
            reasons_bullets = "\n".join(f"- {reason}" for reason in explicit_reasons)
            explicit_section = f"""
使用者明確勾選的拒絕理由：
{reasons_bullets}
請優先把這些理由轉成 DISLIKES_TRAIT。
"""

        reflection_prompt = f"""
你要從使用者的配對接受/拒絕紀錄中萃取可寫入 Graph DB 的偏好三元組。

規則：
- 拒絕與 explicit_reasons 通常產生 DISLIKES_TRAIT。
- 接受或正向回饋可產生 LIKES_TRAIT。
- trait 要短、具體、可重複使用，例如：效率至上、愛打籃球、年長成熟、共同學習。
- 不要輸出候選人 ID 當 trait。
- 沒有明確偏好時輸出空 relationships。

歷史資料：
{history_text}
{explicit_section}

只輸出 JSON：
{{
  "relationships": [
    {{
      "user_id": "使用者 ID",
      "relation_type": "LIKES_TRAIT|DISLIKES_TRAIT",
      "trait": "短 trait",
      "reason": "一句理由"
    }}
  ]
}}
"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是只輸出 JSON 的圖譜記憶萃取器。"},
                    {"role": "user", "content": reflection_prompt},
                ],
                temperature=0.1,
            )
            return response.choices[0].message.content
        except Exception as e:
            return json.dumps({"error": f"graph_reflection_failed: {e}"}, ensure_ascii=False)

    def generate_global_reflection(self, from_big_five, from_context, to_big_five, to_context):
        global_prompt = f"""
請根據一次成功配對，歸納一條可重複使用的全域媒合法則。
不要提 user_id，不要寫成個案流水帳；請抽象成「誰在什麼狀態下適合誰」。
abstract_rule 必須是 10-20 個中文字，最多 30 字；像標籤一樣短，不要長句。
好例子：「行動派配穩定傾聽者」、「共同目標帶動互補」、「高壓狀態適合溫和陪伴」。

A Big Five：{json.dumps(from_big_five, ensure_ascii=False) if isinstance(from_big_five, dict) else from_big_five}
A 近期情境：{from_context}
B Big Five：{json.dumps(to_big_five, ensure_ascii=False) if isinstance(to_big_five, dict) else to_big_five}
B 近期情境：{to_context}

只輸出 JSON：
{{
  "abstract_rule": "30字內短法則",
  "category": "相似型|互補型|情境型"
}}
"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是只輸出 JSON 的全域配對法則歸納器。"},
                    {"role": "user", "content": global_prompt},
                ],
                temperature=0.3,
            )
            return response.choices[0].message.content
        except Exception as e:
            return json.dumps({"error": f"global_reflection_failed: {e}"}, ensure_ascii=False)

