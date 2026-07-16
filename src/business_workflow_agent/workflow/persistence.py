from business_workflow_agent.models import WorkflowCheckpoint, WorkflowRun


def build_checkpoint(run: WorkflowRun) -> WorkflowCheckpoint:
    proposal_tool = None
    if run.proposal is not None:
        proposal_tool = run.proposal.get("tool_name")
    return WorkflowCheckpoint(
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        run_id=run.id,
        version=run.version,
        state=run.state,
        snapshot_redacted={
            "state": run.state,
            "version": run.version,
            "step_count": run.step_count,
            "tool_call_count": run.tool_call_count,
            "tokens_used": run.tokens_used,
            "cost_cents_used": run.cost_cents_used,
            "schema_repair_attempts": run.schema_repair_attempts,
            "pending_fields": run.pending_fields,
            "error_code": run.error_code,
            "tool_name": proposal_tool,
        },
    )
