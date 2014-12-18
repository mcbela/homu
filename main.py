#!/usr/bin/env python3

import github3
import toml
import json
import re
import server
import utils
import logging

class PullReqState:
    num = 0
    approved_by = ''
    priority = 0
    status = ''
    head_sha = ''
    merge_sha = ''
    build_res = {}

    def __init__(self, num, head_sha, status):
        self.num = num
        self.head_sha = head_sha
        self.status = status

    def __repr__(self):
        return 'PullReqState#{}(approved_by={}, priority={}, status={})'.format(
            self.num,
            self.approved_by,
            self.priority,
            self.status,
        )

    def sort_key(self):
        return [
            0 if self.approved_by else 1,
            -self.priority,
            self.num,
        ]

    def __lt__(self, other):
        return self.sort_key() < other.sort_key()

def parse_commands(body, username, state, realtime=False):
    state_changed = False

    for word in re.findall(r'\S+', body):
        found = True

        if word in ['r+', 'r=me']:
            state.approved_by = username

        elif word.startswith('r='):
            state.approved_by = word[len('r='):]

        elif word == 'r-':
            state.approved_by = ''

        elif word.startswith('p='):
            try: state.priority = int(word[len('p='):])
            except ValueError: pass

        elif word == 'retry' and realtime:
            state.status = ''

        else:
            found = False

        if found:
            state_changed = True

    return state_changed

def process_queue(states, repos, repo_cfgs, logger, cfg):
    for repo in repos:
        repo_states = sorted(states[repo.name].values())
        repo_cfg = repo_cfgs[repo.name]
        pending_pulls = 0

        for state in repo_states:
            if state.status == 'pending':
                pending_pulls += 1

            elif state.status == '' and state.approved_by:
                pending_pulls += 1

                assert state.head_sha == repo.pull_request(state.num).head.sha

                master_sha = repo.ref('heads/' + repo_cfg['master_branch']).object.sha
                try:
                    js = utils.github_set_ref(repo, 'heads/' + repo_cfg['tmp_branch'], master_sha, force=True)
                except github3.models.GitHubError:
                    js = repo.create_ref('refs/heads/' + repo_cfg['tmp_branch'], master_sha)

                merge_msg = 'Merge {:.7} into {}\n\nApproved-by: {}'.format(
                    state.head_sha,
                    repo_cfg['tmp_branch'],
                    state.approved_by,
                )

                merge_commit = repo.merge(repo_cfg['tmp_branch'], state.head_sha, merge_msg)

                utils.github_set_ref(repo, 'heads/' + repo_cfg['buildbot_branch'], merge_commit.sha, force=True)

                state.status = 'pending'
                state.build_res = {x: None for x in repo_cfgs[repo.name]['builders']}
                state.merge_sha = merge_commit.sha

                logger.info('Starting build: {}'.format(state.merge_sha))

                url = '' # FIXME
                desc = 'Testing candidate {}...'.format(state.merge_sha)
                repo.create_status(state.head_sha, 'pending', url, desc)

            if pending_pulls >= 1: # FIXME
                break

def main():
    logger = logging.getLogger('homu')

    with open('cfg.toml') as fp:
        cfg = toml.loads(fp.read())

    gh = github3.login(token=cfg['main']['token'])

    states = {}
    repos = []
    repo_cfgs = {}

    queue_handler = lambda: process_queue(states, repos, repo_cfgs, logger, cfg)

    for repo_cfg in cfg['repo']:
        repo = gh.repository(repo_cfg['owner'], repo_cfg['repo'])

        states[repo.name] = {}
        repos.append(repo)
        repo_cfgs[repo.name] = repo_cfg

        for pull in repo.iter_pulls(state='open'):
            try: status = next(repo.iter_statuses(pull.head.sha)).state
            except StopIteration: status = ''

            state = PullReqState(pull.number, pull.head.sha, status)

            repo.iter_statuses(pull.head.sha)

            for comment in pull.iter_comments():
                if (
                    comment.user.login in repo_cfg['reviewers'] and
                    comment.original_commit_id == pull.head.sha
                ):
                    if parse_commands(
                        comment.body,
                        comment.user.login,
                        state,
                        queue_handler,
                    ):
                        queue_handler()

            states[repo.name][pull.number] = state

    server.start(cfg, states, queue_handler, repo_cfgs, repos, logger)

if __name__ == '__main__':
    main()
