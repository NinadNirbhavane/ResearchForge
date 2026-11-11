# CLI layer (RF-007). Workflow commands register themselves here.
from app.cli import (approvals, config, export, research, sources, status, ui,
                     workflows)

# Command registry: name -> setup(subparsers), where setup adds the
# subparser and attaches its handler via set_defaults(handler=...).
# Future commands (diagram, export, usage) are added in their own tasks.
# No fake operations are served.
COMMANDS = {
    "approvals": approvals.setup_approvals,
    "approve": approvals.setup_approve,
    "config": config.setup,
    "drafts": workflows.setup_drafts,
    "export": export.setup,
    "extraction": workflows.setup_extraction,
    "gaps": workflows.setup_gaps,
    "reviews": workflows.setup_reviews,
    "research": research.setup,
    "sources": sources.setup,
    "status": status.setup,
    "synthesis": workflows.setup_synthesis,
    "ui": ui.setup,
}
