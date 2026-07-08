"""
Guardrail 檢測引擎 - 支援多 provider
- openai_moderation：OpenAI 官方 /moderations endpoint（原行為）
- llm_classifier：用 Llama Guard 風格 prompt 餵任何 OpenAI 相容 endpoint
"""

import os

from dotenv import load_dotenv
from openai import OpenAI

from app.services.kb_service import KBService

load_dotenv()

LLAMA_GUARD_TEMPLATE = """Task: Check if there is unsafe content in the User message according to our safety policy.

<BEGIN UNSAFE CONTENT CATEGORIES>
S1: Violent Crimes (武器、攻擊、傷害他人)
S2: Sexual Content (露骨性內容、性剝削)
S3: Self-Harm (自殺、自殘)
S4: Illegal Drugs (毒品、非法藥物)
S5: Harassment (持續騷擾、辱罵、人身攻擊)
S6: Sexual Exploitation (性勒索、未成年性誘導)
<END UNSAFE CONTENT CATEGORIES>

<BEGIN CONVERSATION>
User: {text}
<END CONVERSATION>

Provide your safety assessment for the User message above.
- First line MUST be exactly 'safe' or 'unsafe'.
- If unsafe, second line MUST list violated category codes comma-separated (e.g., S2,S5)."""


class GuardrailEngine:
    def __init__(self):
        self.HARD_BLOCK_RECORDS = KBService.get_hard_block_records()
        print(f"   [ Guardrail ] Initialized with {len(self.HARD_BLOCK_RECORDS)} keyword records.")

        self._provider = os.getenv("GUARDRAIL_PROVIDER", "openai_moderation").lower()
        self._openai_mod_client = None
        self._classifier_adapter = None
        self._classifier_model = None

        if self._provider == "openai_moderation":
            api_key = os.getenv("OPENAI_API_KEY")
            self._openai_mod_client = OpenAI(api_key=api_key, timeout=3.0) if api_key else None
        elif self._provider == "llm_classifier":
            try:
                from app.core.llm_adapters import (
                    get_guardrail_classifier_adapter,
                    get_guardrail_classifier_model,
                )

                self._classifier_adapter = get_guardrail_classifier_adapter()
                self._classifier_model = get_guardrail_classifier_model()
            except Exception as e:
                print(f"   [ Guardrail Warning ] LLM classifier init failed: {e}")

    async def check(self, text: str) -> dict:
        flagged_words = []

        # 1. MySQL 禁詞（中文最穩，永遠先跑）
        for record in self.HARD_BLOCK_RECORDS:
            keyword = record.get("keyword", "")
            if keyword and keyword in text:
                mode = record.get("trigger_mode") or "flag"
                flagged_words.append(keyword)
                if mode == "block":
                    return {
                        "is_blocked": True,
                        "reason": f"Hard-Block: {keyword}",
                        "flagged_words": flagged_words,
                    }

        # 2. 走 provider 對應的第二道
        if self._provider == "llm_classifier" and self._classifier_adapter:
            result = self._check_via_classifier(text)
        else:
            result = self._check_via_openai_moderation(text)

        result["flagged_words"] = flagged_words
        return result

    def _check_via_openai_moderation(self, text: str) -> dict:
        if not self._openai_mod_client:
            return {
                "is_blocked": False,
                "reason": "Moderation skipped (no client)",
                "classifier_flagged": False,
            }
        try:
            response = self._openai_mod_client.moderations.create(input=text)
            result = response.results[0]
            if result.flagged:
                categories = [k for k, v in result.categories.model_dump().items() if v]
                return {
                    "is_blocked": False,
                    "reason": f"Passed (openai mod flagged: {categories})",
                    "classifier_flagged": True,
                    "classifier_categories": str(categories),
                }
        except Exception as e:
            err_str = str(e)
            if "401" in err_str or "invalid_api_key" in err_str or "Incorrect API key" in err_str:
                print("   [ Guardrail ] OpenAI key invalid, disabling moderation for this session.")
                self._openai_mod_client = None
            else:
                print(f"   [ Guardrail Warning ] OpenAI Mod Error: {e}")
        return {
            "is_blocked": False,
            "reason": "Passed (openai_moderation)",
            "classifier_flagged": False,
        }

    def _check_via_classifier(self, text: str) -> dict:
        prompt = LLAMA_GUARD_TEMPLATE.format(text=text)
        try:
            resp = self._classifier_adapter.generate(prompt, model=self._classifier_model)
            lines = [ln.strip() for ln in (resp or "").splitlines() if ln.strip()]
            if not lines:
                return {"is_blocked": False, "reason": "Classifier empty response"}
            verdict = lines[0].lower()
            if verdict.startswith("unsafe"):
                categories = lines[1] if len(lines) > 1 else "unknown"
                return {
                    "is_blocked": False,
                    "reason": f"Passed (classifier flagged: {categories})",
                    "classifier_flagged": True,
                    "classifier_categories": categories,
                }
        except Exception as e:
            print(f"   [ Guardrail Warning ] Classifier Error: {e}")
        return {
            "is_blocked": False,
            "reason": "Passed (llm_classifier)",
            "classifier_flagged": False,
        }
