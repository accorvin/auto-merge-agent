from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .config import ConfigStore
from .llm import LangChainJudge
from .plugins import ForgePlugin, ReviewerPlugin, load_plugin
from .rules import path_matches, rule_evaluator
from .types import (
    ChangedFile, Check, CheckSelector, Decision, LoadedConfig, PullRequestRef, PullRequestSnapshot,
    RISK_ORDER, SEVERITY_ORDER, ReviewArtifact, RuleConfig,
)

logger = logging.getLogger(__name__)


class EvaluationState(TypedDict, total=False):
    ref: PullRequestRef
    config: LoadedConfig
    pr: PullRequestSnapshot
    files: list
    checks: list[Check]
    artifacts: list[ReviewArtifact]
    candidates: list[RuleConfig]
    passed_checks: list[str]
    decision: Decision


def _matches(check: Check, selector: CheckSelector) -> bool:
    return check.name == selector.name and (selector.source is None or check.source == selector.source)


def _relevant_ci_checks(checks: list[Check], config: LoadedConfig) -> list[Check]:
    return [
        check for check in checks
        if check.name != "automerge/policy"
        and not any(_matches(check, item) for item in config.policy.ci.ignore)
    ]


def _counted_lines(files: list[ChangedFile], excluded_paths: list[str]) -> int:
    return sum(
        file.additions + file.deletions for file in files
        if not any(path_matches(file.path, pattern) for pattern in excluded_paths)
    )


def _ci_decision(checks: list[Check], config: LoadedConfig) -> tuple[str, str]:
    policy = config.policy.ci
    relevant = _relevant_ci_checks(checks, config)
    for selector in policy.expected:
        if not any(_matches(check, selector) for check in relevant):
            return "waiting", f"Waiting for expected CI check: {selector.name}"
    for check in relevant:
        if check.status == "completed" and check.conclusion != "success":
            return "human_review", f"CI check {check.name} concluded {check.conclusion}"
    if not relevant or sum(check.conclusion == "success" for check in relevant) < policy.min_successful_checks:
        return "waiting", "Waiting for at least one successful CI check"
    for check in relevant:
        if check.status != "completed" or check.conclusion is None:
            return "waiting", f"Waiting for CI check: {check.name}"
    return "eligible", "CI passed"


def _human_change_request(artifacts: list[ReviewArtifact]) -> bool:
    latest: dict[str, ReviewArtifact] = {}
    for artifact in artifacts:
        if artifact.kind != "review" or artifact.author.endswith("[bot]"):
            continue
        if artifact.author not in latest or (artifact.updated_at or "") > (latest[artifact.author].updated_at or ""):
            latest[artifact.author] = artifact
    return any(a.state == "CHANGES_REQUESTED" for a in latest.values())


class EvaluationEngine:
    def __init__(self, store: ConfigStore, forge: ForgePlugin, reviewer: ReviewerPlugin | None = None, judge: LangChainJudge | None = None):
        self.store, self.forge, self.reviewer, self.judge = store, forge, reviewer, judge
        graph = StateGraph(EvaluationState)
        graph.add_node("fetch", self._fetch)
        graph.add_node("static", self._static)
        graph.add_node("llm", self._llm)
        graph.add_edge(START, "fetch")
        graph.add_edge("fetch", "static")
        graph.add_conditional_edges("static", lambda state: "llm" if state.get("candidates") else END)
        graph.add_edge("llm", END)
        self.graph = graph.compile()

    async def _fetch(self, state: EvaluationState) -> dict[str, Any]:
        pr = await self.forge.get_pull_request(state["ref"])
        files = await self.forge.get_changed_files(pr)
        checks = await self.forge.get_checks(pr)
        artifacts = await self.forge.get_review_artifacts(pr)
        return {"pr": pr, "files": files, "checks": checks, "artifacts": artifacts}

    async def _static(self, state: EvaluationState) -> dict[str, Any]:
        pr, config, files = state["pr"], state["config"], state["files"]

        def result(kind: str, reason: str, matched: str | None = None, passed_checks: list[str] | None = None):
            return {"decision": Decision(state=kind, head_sha=pr.head_sha, reasons=[reason], matched_rule=matched, config_revision=config.revision, passed_checks=passed_checks or [])}

        if not config.policy.enabled:
            return result("human_review", "Automerge is disabled for this repository")
        if pr.state != "open" or pr.draft:
            return result("human_review", "PR is closed or draft")
        if pr.author_login.lower() not in config.trusted_authors:
            return result("human_review", f"PR author @{pr.author_login or 'unknown'} is not a trusted author")
        blocked = [label for label in pr.labels if label.lower().startswith("do-not-merge") or label.lower() in {"hold", "work-in-progress", "needs-ok-to-test", "needs-rebase"}]
        if blocked:
            return result("human_review", f"PR has blocking label: {blocked[0]}")
        if pr.mergeable is None:
            return result("waiting", "GitHub has not calculated mergeability yet")
        if not pr.mergeable:
            return result("human_review", "GitHub reports that this PR cannot merge")
        if len(files) != pr.changed_files or not files:
            return result("human_review", "Changed-file list is incomplete")
        if _human_change_request(state["artifacts"]):
            return result("human_review", "A human reviewer requested changes")
        max_lines = config.policy.limits.max_changed_lines
        counted = _counted_lines(files, config.policy.limits.exclude_from_line_count)
        if max_lines is not None and counted > max_lines:
            return result("human_review", f"Changed lines {counted} exceed limit {max_lines}")
        ci_state, ci_reason = _ci_decision(state["checks"], config)
        if ci_state != "eligible":
            return result(ci_state, ci_reason)
        reviewer = self.reviewer or load_plugin("reviewers", config.policy.review.plugin)()
        assessment = reviewer.assess(pr, state["artifacts"])
        if assessment.state == "waiting":
            return result("waiting", assessment.reason)
        if assessment.state != "complete" or assessment.covered_head_sha != pr.head_sha or assessment.merge_risk is None:
            return result("human_review", assessment.reason)
        if RISK_ORDER.index(assessment.merge_risk) > RISK_ORDER.index(config.policy.review.max_merge_risk):
            return result("human_review", f"Review merge risk {assessment.merge_risk.value} exceeds {config.policy.review.max_merge_risk.value}")
        for finding in assessment.findings:
            if SEVERITY_ORDER.index(finding.severity) > SEVERITY_ORDER.index(config.policy.review.max_finding_severity):
                return result("human_review", f"Review finding {finding.severity.value} exceeds {config.policy.review.max_finding_severity.value}: {finding.url}")
        passed = [
            f"PR open, ready, mergeable, and free of blocking labels or human change requests",
            f"Trusted author @{pr.author_login}",
            f"Changed-file list complete: {len(files)} files",
            f"Changed lines: {counted}" + (f" <= repository limit {max_lines}" if max_lines is not None else ""),
        ]
        for check in _relevant_ci_checks(state["checks"], config):
            location = f" at {check.sha[:12]}" if check.sha else ""
            passed.append(f"CI {check.name} ({check.source}) passed{location}")
        finding_summary = (
            "no findings" if not assessment.findings else
            f"{len(assessment.findings)} findings, all <= {config.policy.review.max_finding_severity.value}"
        )
        reviewer_name = "CodeRabbit" if config.policy.review.plugin == "coderabbit" else config.policy.review.plugin
        passed.append(
            f"{reviewer_name} reviewed current HEAD {pr.head_sha[:12]}; "
            f"merge risk {assessment.merge_risk.value} <= {config.policy.review.max_merge_risk.value}; {finding_summary}"
        )
        matching = [rule for rule in config.policy.rules if rule_evaluator(rule.type).evaluate(rule, files).state == "pass"]
        if not matching:
            return result("human_review", "No eligibility rule matched")
        no_llm = next((rule for rule in matching if rule.llm is None), None)
        if no_llm:
            return result("eligible", f"Rule {no_llm.id} matched", no_llm.id, passed + [f"Static eligibility rule {no_llm.id} matched"])
        return {"candidates": matching, "passed_checks": passed}

    async def _llm(self, state: EvaluationState) -> dict[str, Any]:
        pr, config = state["pr"], state["config"]
        reasons = []
        counted = _counted_lines(state["files"], config.policy.limits.exclude_from_line_count)
        candidates = []
        cap_checks: dict[str, list[str]] = {}
        for rule in state["candidates"]:
            passed_caps = []
            for section_name in ("complexity", "custom"):
                section = getattr(rule.llm, section_name)
                cap = section.max_changed_lines if section else None
                if cap is not None and counted > cap:
                    reasons.append(f"Rule {rule.id}: {section_name} LLM limit {cap} exceeded by {counted} changed lines")
                    break
                if cap is not None:
                    passed_caps.append(f"{section_name} LLM changed lines: {counted} <= {cap}")
            else:
                candidates.append(rule)
                cap_checks[rule.id] = passed_caps
        if not candidates:
            return {"decision": Decision(state="human_review", head_sha=pr.head_sha, reasons=reasons, config_revision=config.revision)}
        if self.judge is None:
            return {"decision": Decision(state="human_review", head_sha=pr.head_sha, reasons=reasons + ["LLM evaluator unavailable"], config_revision=config.revision)}
        waiting = False
        for rule in candidates:
            verdict = await self.judge.evaluate(rule.llm, state["files"], pr.title, pr.body)
            if verdict.state == "pass":
                llm_checks = verdict.passed_checks or [
                    f"LLM {name} criterion passed ({getattr(rule.llm, name).model})"
                    for name in ("complexity", "custom") if getattr(rule.llm, name)
                ]
                checklist = state["passed_checks"] + cap_checks[rule.id] + llm_checks + [f"Eligibility rule {rule.id} matched"]
                return {"decision": Decision(state="eligible", head_sha=pr.head_sha, reasons=[f"Rule {rule.id} and LLM gates passed"], matched_rule=rule.id, config_revision=config.revision, passed_checks=checklist)}
            reasons.append(f"Rule {rule.id}: {verdict.reason}")
            waiting |= verdict.state == "wait"
        return {"decision": Decision(state="waiting" if waiting else "human_review", head_sha=pr.head_sha, reasons=reasons, config_revision=config.revision)}

    async def evaluate(self, ref: PullRequestRef) -> tuple[PullRequestSnapshot, Decision]:
        config = self.store.load(ref.host, ref.owner, ref.repo)
        state = await self.graph.ainvoke({"ref": ref, "config": config})
        return state["pr"], state["decision"]

    async def act(self, ref: PullRequestRef, active: bool = False) -> Decision:
        pr, decision = await self.evaluate(ref)
        config = self.store.load(ref.host, ref.owner, ref.repo)
        if not active or pr.state != "open":
            return decision
        await self.forge.publish_decision(pr, decision, config.policy.human_review.mention, config.policy.human_review.comment)
        if decision.state != "eligible":
            return decision
        # Re-evaluate all gates before a write. The merge API separately checks the head SHA.
        latest_pr, latest = await self.evaluate(ref)
        if latest.state != "eligible" or latest.head_sha != decision.head_sha or latest.config_revision != decision.config_revision:
            await self.forge.publish_decision(latest_pr, latest, config.policy.human_review.mention, config.policy.human_review.comment)
            return latest
        if latest_pr.base_sha != pr.base_sha:
            changed = Decision(state="waiting", head_sha=latest.head_sha, reasons=["Base branch changed during evaluation"], config_revision=latest.config_revision)
            await self.forge.publish_decision(latest_pr, changed, config.policy.human_review.mention, config.policy.human_review.comment)
            return changed
        latest = latest.model_copy(update={"passed_checks": latest.passed_checks + [
            f"Final recheck confirmed HEAD {latest.head_sha[:12]} and base {latest_pr.base_sha[:12]} are unchanged"
        ]})
        await self.forge.publish_merge_checklist(latest_pr, latest, "attempting")
        try:
            merged = await self.forge.merge(latest_pr, config.policy.merge.method)
        except Exception as exc:
            failed = Decision(state="waiting", head_sha=latest.head_sha, reasons=[f"Merge attempt failed: {type(exc).__name__}"], config_revision=latest.config_revision)
            await self.forge.publish_decision(latest_pr, failed, config.policy.human_review.mention, config.policy.human_review.comment)
            try:
                await self.forge.publish_merge_checklist(latest_pr, latest, "aborted")
            except Exception:
                logger.exception("Could not update merge checklist after failed merge for %s#%s", ref.slug, ref.number)
            raise
        if not merged:
            changed = Decision(state="waiting", head_sha=latest.head_sha, reasons=["PR head changed before merge; reevaluation required"], config_revision=latest.config_revision)
            await self.forge.publish_decision(latest_pr, changed, config.policy.human_review.mention, config.policy.human_review.comment)
            await self.forge.publish_merge_checklist(latest_pr, latest, "aborted")
            return changed
        try:
            await self.forge.publish_merge_checklist(latest_pr, latest, "merged")
        except Exception:
            logger.exception("Could not mark merge checklist complete for %s#%s", ref.slug, ref.number)
        return latest
