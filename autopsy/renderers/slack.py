"""Slack renderer for posting diagnoses via Incoming Webhook."""

from __future__ import annotations

import json
import urllib.request

from autopsy.ai.models import DiagnosisResult  # noqa: TC001 — used at runtime
from autopsy.utils.errors import SlackSendError


class SlackRenderer:
    """Post diagnosis to Slack via Incoming Webhook."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def render(self, result: DiagnosisResult) -> bool:
        """
        Post diagnosis to Slack. Returns True on success.

        Uses Block Kit for rich formatting.
        """
        blocks = self._build_blocks(result)
        payload = json.dumps({"blocks": blocks}).encode("utf-8")

        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # type: ignore[call-overload]
                status = getattr(resp, "status", None)
                if status == 200:
                    return True
                raise SlackSendError(
                    message=f"Slack webhook returned non-200 status: {status}",
                    hint="Check that the webhook URL is valid and active.",
                )
        except SlackSendError:
            raise
        except Exception as exc:
            raise SlackSendError(
                message=f"Failed to send to Slack: {exc}",
                hint=(
                    "Check your webhook URL in config. Generate one at: "
                    "https://api.slack.com/messaging/webhooks"
                ),
            ) from exc

    def _build_blocks(self, result: DiagnosisResult) -> list[dict]:
        """Build Slack Block Kit blocks from DiagnosisResult."""
        rc = result.root_cause
        deploy = result.correlated_deploy
        fix = result.suggested_fix

        conf_pct = int(rc.confidence * 100)
        if rc.confidence >= 0.75:
            conf_emoji = "🟢"
        elif rc.confidence >= 0.5:
            conf_emoji = "🟡"
        else:
            conf_emoji = "🔴"

        # Evidence (max 5 lines)
        evidence_items = rc.evidence[:5] if rc.evidence else ["(no evidence items captured)"]
        evidence_text = "*Evidence*\n" + "\n".join(f"• {e}" for e in evidence_items)

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🚨 AUTOPSY — Incident Diagnosis"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Root Cause* ({conf_emoji} {conf_pct}% confidence · `{rc.category}`)\n"
                        f"{rc.summary}"
                    ),
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": evidence_text,
                },
            },
        ]

        # Correlated Deploy (only if present)
        if deploy and deploy.commit_sha:
            files_str = (
                ", ".join(deploy.changed_files[:5]) if deploy.changed_files else "—"
            )
            pr_title = deploy.pr_title or "No PR title"
            author = deploy.author or "unknown"
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*Correlated Deploy*\n"
                            f"`{deploy.commit_sha[:8]}` — \"{pr_title}\"\n"
                            f"Author: @{author} · Files: {files_str}"
                        ),
                    },
                }
            )

        # Suggested fix
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Suggested Fix*\n"
                        f"🔴 *Now:* {fix.immediate}\n"
                        f"🟢 *Long-term:* {fix.long_term}"
                    ),
                },
            }
        )

        # Timeline (compact, max 6 items)
        if result.timeline:
            timeline_text = "\n".join(
                f"{e.time} — {e.event}" for e in result.timeline[:6]
            )
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Timeline*\n{timeline_text}",
                    },
                }
            )

        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "_Generated by AUTOPSY CLI · github.com/zeelapatel/autopsy_",
                    }
                ],
            }
        )
        return blocks

