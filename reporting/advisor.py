"""Advisor transform from checks to AI-facing report fields."""


def recommend(validation, settings):
    if validation.valid:
        return ("ready", "ready_to_apply", "Run sync with --apply when ready.")
    if _has_policy(validation, "POL-TOP-002"):
        return ("blocked", "target_dirty", "Review and clean target before syncing.")
    return ("blocked", "policy_block", "Resolve blocking risks before applying.")


def suggest_commands(plan, validation, settings):
    commands = []
    if _has_policy(validation, "POL-TOP-002"):
        dest = str(plan.dest)
        commands.extend([
            "git -C {dest} status --short".format(dest=dest),
            "git -C {dest} diff".format(dest=dest),
        ])
        strategy = settings.command_style.dirty_target_strategy
        if strategy == "stash":
            commands.append("git -C {dest} stash".format(dest=dest))
        elif strategy == "commit":
            message = settings.command_style.commit_message_template.format(target=plan.target_name)
            commands.append("git -C {dest} add -A".format(dest=dest))
            commands.append("git -C {dest} commit -m {message!r}".format(dest=dest, message=message))
    elif validation.valid:
        commands.append("sync-worktree sync {target} --apply".format(target=plan.target_name))

    return _filter_denied(commands, settings)


def suggest_workflow(plan, validation, settings):
    if validation.valid:
        return [
            "review report",
            "run sync-worktree sync {target} --apply".format(target=plan.target_name),
        ]

    if _has_policy(validation, "POL-TOP-002"):
        strategy = settings.command_style.dirty_target_strategy
        return [
            "review target dirtiness",
            "{strategy} target changes according to config.command_style".format(strategy=strategy),
            "rerun sync-worktree check {target}".format(target=plan.target_name),
        ]

    return [
        "review blocking risks",
        "update config or source state",
        "rerun sync-worktree check {target}".format(target=plan.target_name),
    ]


def _has_policy(validation, code):
    return any((not result.valid) and result.code == code for result in validation.results)


def _filter_denied(commands, settings):
    denied = settings.command_style.deny_commands
    return [
        command for command in commands
        if not any(pattern in command for pattern in denied)
    ]
