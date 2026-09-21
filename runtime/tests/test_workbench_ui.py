"""Static safety checks for the ADR-004 Iteration 1 workbench interaction.

The prototype is intentionally static HTML/JavaScript, so these checks protect
its human-facing safety copy and ensure task handling cannot silently regress
back to browser ``prompt()`` input.
"""
from pathlib import Path


WORKBENCH = Path(__file__).resolve().parents[2] / "prototype" / "workbench.html"


def test_task_handling_uses_a_form_modal_with_safety_copy():
    page = WORKBENCH.read_text(encoding="utf-8")

    assert 'id="task-modal"' in page
    assert 'id="task-modal-form"' in page
    assert 'id="task-modal-input"' in page
    assert 'id="task-modal-success"' in page
    assert 'prompt(' not in page
    assert "evidenceRefsFrom" in page
    assert "提交证据不代表满足" in page
    assert "不粘贴企业私有材料正文" in page
    assert "待核验与重算" in page


def test_task_modal_preserves_cancel_escape_and_request_traceability():
    page = WORKBENCH.read_text(encoding="utf-8")

    assert 'id="btn-task-modal-cancel"' in page
    assert 'e.key === "Escape"' in page
    assert 'closeTaskModal' in page
    assert "submit.disabled = true" in page
    assert '$("btn-task-modal-cancel").textContent = "关闭"' in page
    assert "请求编号：${d.request_id" in page
    assert "tasks/${encodeURIComponent(taskId)}/${endpoint}" in page
