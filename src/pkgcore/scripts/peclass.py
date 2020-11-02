"""eclass querying and conversion utility"""

import os

from pkgcore.util import commandline


argparser = commandline.ArgumentParser(description=__doc__, script=(__file__, __name__))
argparser.add_argument(
    'eclasses', metavar='eclass', nargs='*', help='eclasses to target')

# TODO: allow multi-repo comma-separated input
target_opts = argparser.add_argument_group('target options')
target_opts.add_argument(
    '-r', '--repo', dest='selected_repo', metavar='REPO', priority=29,
    action=commandline.StoreRepoObject,
    repo_type='all-raw', allow_external_repos=True,
    help='repo to source eclasses from')
@argparser.bind_delayed_default(30, 'repo')
def _setup_repos(namespace, attr):
    target_repo = namespace.selected_repo
    all_ebuild_repos = namespace.domain.all_ebuild_repos_raw
    namespace.cwd = os.getcwd()

    # TODO: move this to StoreRepoObject
    if target_repo is None:
        # determine target repo from the target directory
        for repo in all_ebuild_repos.trees:
            if namespace.cwd in repo:
                target_repo = repo
                break
        else:
            # determine if CWD is inside an unconfigured repo
            target_repo = namespace.domain.find_repo(
                namespace.cwd, config=namespace.config)

    # fallback to using all, unfiltered ebuild repos if no target repo can be found
    namespace.repo = target_repo if target_repo is not None else all_ebuild_repos


@argparser.bind_final_check
def _validate_args(parser, namespace):
    if not namespace.eclasses:
        if namespace.selected_repo:
            print(namespace.selected_repo)
        else:
            print('asdfsadf')


@argparser.bind_main_func
def main(options, out, err):
    print(options)
