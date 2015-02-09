# Copyright: 2006-2011 Brian Harring <ferringb@gmail.com>
# Copyright: 2006 Marien Zwart <marienz@gentoo.org>
# License: BSD/GPL2

"""pkgcore package merging and unmerging interface

pmerge is the main command-line utility for merging and unmerging packages on a
system. It provides an interface to install, update, and uninstall ebuilds from
source or binary packages.
"""

# more should be doc'd...
__all__ = ("argparser", "AmbiguousQuery", "NoMatches")

import argparse
from time import time

from pkgcore.ebuild import resolver
from pkgcore.ebuild.atom import atom
from pkgcore.merge import errors as merge_errors
from pkgcore.operations import observer, format
from pkgcore.resolver.util import reduce_to_failures
from pkgcore.restrictions import packages
from pkgcore.restrictions.boolean import OrRestriction
from pkgcore.util import commandline, parserestrict, repo_utils

from snakeoil.compatibility import IGNORED_EXCEPTIONS
from snakeoil.currying import partial
from snakeoil.lists import stable_unique


class StoreTarget(argparse._AppendAction):

    def __call__(self, parser, namespace, values, option_string=None):
        if isinstance(values, basestring):
            values = [values]
        for x in values:
            if x.startswith('@'):
                ret = parser._parse_known_args(['--set', x[1:]], namespace)
                if ret[1]:
                    raise RuntimeError(
                        "failed parsing %r, %r, got back %r" %
                        (option_string, values, ret[1]))
            else:
                argparse._AppendAction.__call__(
                    self, parser, namespace,
                    parserestrict.parse_match(x), option_string=option_string)


argparser = commandline.mk_argparser(
    domain=True, description=__doc__.split('\n', 1)[0])
argparser.add_argument(
    nargs='*', dest='targets', action=StoreTarget,
    help="extended atom matching of packages")

query_options = argparser.add_argument_group("Package querying options")
query_options.add_argument(
    '-N', '--newuse', action='store_true',
    help="check for changed useflags in installed packages "
         "(implies -1)")
query_options.add_argument(
    '-s', '--set', store_name=True,
    action=commandline.StoreConfigObject, type=str, priority=35,
    config_type='pkgset', help='specify a pkgset to use')

merge_mode = argparser.add_argument_group('Available operations')
merge_mode.add_argument(
    '-C', '--unmerge', action='store_true',
    help='unmerge a package')
merge_mode.add_argument(
    '--clean', action='store_true',
    help='Remove installed packages that are not referenced by any '
         'target packages/sets; defaults to -s world -s system if no targets '
         'are specified. Use with *caution*, this option used incorrectly '
         'can render your system unusable. Note that this implies --deep.')
merge_mode.add_argument(
    '-p', '--pretend', action='store_true',
    help="do the resolution, but don't merge/fetch anything")
merge_mode.add_argument(
    '--ignore-failures', action='store_true',
    help='ignore resolution failures')
merge_mode.add_argument(
    '-a', '--ask', action='store_true',
    help="do the resolution, but ask to merge/fetch anything")
merge_mode.add_argument(
    '--force', action='store_true',
    dest='force',
    help="force merging to a repo, regardless of if it's frozen")
merge_mode.add_argument(
    '-f', '--fetchonly', action='store_true',
    help="do only the fetch steps of the resolved plan")
merge_mode.add_argument(
    '-1', '--oneshot', action='store_true',
    help="do not record changes in the world file; if a set is "
         "involved, defaults to forcing oneshot")

resolution_options = argparser.add_argument_group("Resolver options")
resolution_options.add_argument(
    '-u', '--upgrade', action='store_true',
    help='try to upgrade already installed packages/dependencies')
resolution_options.add_argument(
    '-D', '--deep', action='store_true',
    help='force the resolver to verify already installed dependencies')
resolution_options.add_argument(
    '--preload-vdb-state', action='store_true',
    help="Enable preloading of the installed packages database. "
         "This causes the resolver to work with a complete graph, thus "
         "disallowing actions that conflict with installed packages. If "
         "disabled, it's possible for the requested action to conflict with "
         "already installed dependencies that aren't involved in the graph of "
         "the requested operation.")
resolution_options.add_argument(
    '-i', '--ignore-cycles', action='store_true',
    help="Ignore cycles if they're found to be unbreakable; "
         "a depends on b, and b depends on a, with neither built is an "
         "example.")
resolution_options.add_argument(
    '--with-bdeps', action='store_true',
    help="whether or not to process build dependencies for pkgs that "
         "are already built; defaults to ignoring them")
resolution_options.add_argument(
    '-O', '--nodeps', action='store_true',
    help='disable dependency resolution')
resolution_options.add_argument(
    '-n', '--noreplace', action='store_false', dest='replace',
    help="don't reinstall target atoms if they're already installed")
resolution_options.add_argument(
    '-b', '--buildpkg', action='store_true',
    help="build binary packages")
resolution_options.add_argument(
    '-B', '--buildpkgonly', action='store_true',
    help="only build binary packages without merging to the filesystem")
resolution_options.add_argument(
    '-k', '--usepkg', action='store_true',
    help="prefer to use binpkgs")
resolution_options.add_argument(
    '-K', '--usepkgonly', action='store_true',
    help="use only built packages")
resolution_options.add_argument(
    '-S', '--source-only', action='store_true',
    help="use source packages only; no pre-built packages used")
resolution_options.add_argument(
    '-e', '--empty', action='store_true',
    help="force rebuilding of all involved packages, using installed "
         "packages only to satisfy building the replacements")

output_options = argparser.add_argument_group("Output related options")
output_options.add_argument(
    '-v', '--verbose', action='store_true',
    help="be verbose in output")
output_options.add_argument(
    '--quiet-repo-display', action='store_true',
    help="In the package merge list display, suppress ::repository "
         "output, and instead use numbers to indicate which repositories "
         "packages come from.")
output_options.add_argument(
    '-F', '--formatter', priority=90,
    action=commandline.StoreConfigObject, get_default=True,
    config_type='pmerge_formatter',
    help='which formatter to output --pretend or --ask output through.')


class AmbiguousQuery(parserestrict.ParseError):
    def __init__(self, token, keys):
        parserestrict.ParseError.__init__(
            self, '%s: multiple matches (%s)' % (token, ', '.join(keys)))
        self.token = token
        self.keys = keys


class NoMatches(parserestrict.ParseError):
    def __init__(self, token):
        parserestrict.ParseError.__init__(self, '%s: no matches' % (token,))


class Failure(ValueError):
    """Raised internally to indicate an "expected" failure condition."""


def unmerge(out, err, vdb, restrictions, options, formatter, world_set=None):
    """Unmerge tokens. hackish, should be rolled back into the resolver"""
    all_matches = set()
    for restriction in restrictions:
        # Catch restrictions matching across more than one category.
        # Multiple matches in the same category are acceptable.

        # The point is that matching across more than one category is
        # nearly always unintentional ("pmerge -C spork" without
        # realising there are sporks in more than one category), but
        # matching more than one cat/pkg is impossible without
        # explicit wildcards.
        matches = vdb.match(restriction)
        if not matches:
            raise Failure('Nothing matches %s' % (restriction,))
        categories = set(pkg.category for pkg in matches)
        if len(categories) > 1:
            raise parserestrict.ParseError(
                '%s is in multiple categories (%s)' % (
                    restriction, ', '.join(set(pkg.key for pkg in matches))))
        all_matches.update(matches)

    matches = sorted(all_matches)
    out.write(out.bold, 'The following packages are to be unmerged:')
    out.prefix = [out.bold, ' * ', out.reset]
    for match in matches:
        out.write(match.cpvstr)
    out.prefix = []

    repo_obs = observer.repo_observer(observer.formatter_output(out), not options.debug)

    if options.pretend:
        return

    if (options.ask and not formatter.ask("Would you like to unmerge these packages?")):
        return
    return do_unmerge(options, out, err, vdb, matches, world_set, repo_obs)


def do_unmerge(options, out, err, vdb, matches, world_set, repo_obs):
    if vdb.frozen:
        if options.force:
            out.write(
                out.fg('red'), out.bold,
                'warning: vdb is frozen, overriding')
            vdb.frozen = False
        else:
            raise Failure('vdb is frozen')

    for idx, match in enumerate(matches):
        out.write("removing %i of %i: %s" % (idx + 1, len(matches), match))
        out.title("%i/%i: %s" % (idx + 1, len(matches), match))
        op = options.domain.uninstall_pkg(match, observer=repo_obs)
        ret = op.finish()
        if not ret:
            if not options.ignore_failures:
                raise Failure('failed unmerging %s' % (match,))
            out.write(out.fg('red'), 'failed unmerging ', match)
        update_worldset(world_set, match, remove=True)
    out.write("finished; removed %i packages" % len(matches))


def display_failures(out, sequence, first_level=True, debug=False):
    """when resolution fails, display a nicely formatted message"""

    sequence = iter(sequence)
    frame = sequence.next()
    if first_level:
        # pops below need to exactly match.
        out.first_prefix.extend((out.fg("red"), "!!!", out.reset))
    out.first_prefix.append(" ")
    out.write("request %s, mode %s" % (frame.atom, frame.mode))
    for pkg, steps in sequence:
        out.write("trying %s" % str(pkg.cpvstr))
        out.first_prefix.append(" ")
        for step in steps:
            if isinstance(step, list):
                display_failures(out, step, False, debug=debug)
            elif step[0] == 'reduce':
                out.write("removing choices involving %s" %
                          ', '.join(str(x) for x in step[1]))
            elif step[0] == 'blocker':
                out.write("blocker %s failed due to %s existing" % (step[1],
                          ', '.join(str(x) for x in step[2])))
            elif step[0] == 'cycle':
                out.write("%s cycle on %s: %s" % (step[1].mode, step[1].atom, step[2]))
            elif step[0] == 'viable' and not step[1]:
                out.write("%s: failed %s" % (step[3], step[4]))
            elif step[0] == 'choice':
                if not step[2]:
                    out.write("failed due to %s" % (step[3],))
            elif step[0] == "debug":
                if debug:
                    out.write(step[1])
            else:
                out.write(step)
        out.first_prefix.pop()
    out.first_prefix.pop()
    if first_level:
        for x in xrange(3):
            out.first_prefix.pop()


def slotatom_if_slotted(repos, checkatom):
    """check repos for more than one slot of given atom"""

    if checkatom.slot is None or checkatom.slot[0] != "0":
        return checkatom

    found_slots = ()
    pkgs = repos.itermatch(checkatom, sorter=sorted)
    for pkg in pkgs:
        found_slots.update(pkg.slot[0])

    if len(found_slots) == 1:
        return atom(checkatom.key)

    return checkatom


def update_worldset(world_set, pkg, remove=False):
    """record/kill given atom in worldset"""

    if world_set is None:
        return
    if remove:
        try:
            world_set.remove(pkg)
        except KeyError:
            # nothing to remove, thus skip the flush
            return
    else:
        world_set.add(pkg)
    world_set.flush()


@argparser.bind_final_check
def _validate(parser, namespace):
    if namespace.unmerge:
        if namespace.set:
            parser.error("Using sets with -C probably isn't wise, aborting")
        if namespace.upgrade:
            parser.error("Cannot upgrade and unmerge simultaneously")
        if not namespace.targets:
            parser.error("You must provide at least one atom")
        if namespace.clean:
            parser.error("Cannot use -C with --clean")
    if namespace.clean:
        if namespace.set or namespace.targets:
            parser.error("--clean currently cannot be used w/ any sets or "
                         "targets given")
        namespace.set = [(x, namespace.config.pkgset[x]) for x in ('world', 'system')]
        namespace.deep = True
        if namespace.usepkgonly or namespace.usepkg or namespace.source_only:
            parser.error(
                '--clean cannot be used with any of the following namespace: '
                '--usepkg --usepkgonly --source-only')
    elif namespace.usepkgonly and namespace.usepkg:
        parser.error('--usepkg is redundant when --usepkgonly is used')
    elif (namespace.usepkgonly or namespace.usepkg) and namespace.source_only:
        parser.error("--source-only cannot be used with --usepkg nor --usepkgonly")
    if namespace.set:
        namespace.replace = False
    if not namespace.targets and not namespace.set and not namespace.newuse:
        parser.error('Need at least one atom/set')
    if namespace.newuse:
        namespace.oneshot = True

    # At some point, fix argparse so this isn't necessary...
    def f(val):
        if val is None:
            return ()
        elif isinstance(val, tuple):
            return [val]
        return val
    namespace.targets = f(namespace.targets)
    namespace.set = f(namespace.set)


def parse_atom(restriction, repo, livefs_repos, return_none=False):
    """Use :obj:`parserestrict.parse_match` to produce a single atom.

    This matches the restriction against a repo. If multiple pkgs match, then
    the restriction is applied against installed repos skipping pkgs from the
    'virtual' category. If multiple pkgs still match the restriction,
    AmbiguousQuery is raised otherwise the matched atom is returned.

    :param restriction: string to convert.
    :param repo: :obj:`pkgcore.repository.prototype.tree` instance to search in.
    :param livefs_repos: :obj:`pkgcore.config.domain.all_livefs_repos` instance to search in.
    :param return_none: indicates if no matches raises or returns C{None}

    :return: an atom or C{None}.
    """
    key_matches = set(x.key for x in repo.itermatch(restriction))
    if not key_matches:
        raise NoMatches(restriction)
    elif len(key_matches) > 1:
        installed_matches = set(x.key for x in livefs_repos.itermatch(restriction)
                                if x.category != 'virtual')
        if len(installed_matches) == 1:
            restriction = atom(installed_matches.pop())
        else:
            raise AmbiguousQuery(restriction, sorted(key_matches))
    if isinstance(restriction, atom):
        # atom is guaranteed to be fine, since it's cat/pkg
        return restriction
    return packages.KeyedAndRestriction(restriction, key=key_matches.pop())


@argparser.bind_delayed_default(50, name='world')
def load_world(namespace, attr):
    value = namespace.config.pkgset['world']
    setattr(namespace, attr, value)
    return value


@argparser.bind_main_func
def main(options, out, err):
    config = options.config
    if options.debug:
        resolver.plan.limiters.add(None)

    domain = options.domain
    livefs_repos = domain.all_livefs_repos
    world_set = world_list = options.world
    if options.oneshot:
        world_set = None

    formatter = options.formatter(
        out=out, err=err,
        unstable_arch=domain.unstable_arch,
        domain_settings=domain.settings,
        use_expand=domain.profile.use_expand,
        use_expand_hidden=domain.profile.use_expand_hidden,
        pkg_get_use=domain.get_package_use_unconfigured,
        world_list=world_list,
        verbose=options.verbose,
        livefs_repos=livefs_repos,
        distdir=domain.fetcher.get_storage_path(),
        quiet_repo_display=options.quiet_repo_display)

    # This mode does not care about sets and packages so bypass all that.
    if options.unmerge:
        if not options.oneshot:
            if world_set is None:
                err.write("Disable world updating via --oneshot, or fix your configuration")
                return 1
        try:
            unmerge(out, err, livefs_repos, options.targets, options, formatter, world_set)
        except (parserestrict.ParseError, Failure) as e:
            out.error(str(e))
            return 1
        return

    source_repos = domain.source_repositories
    installed_repos = domain.installed_repositories

    if options.usepkgonly:
        source_repos = source_repos.change_repos(
            x for x in source_repos
            if getattr(x, 'repository_type', None) != 'source')
    elif options.usepkg:
        repo_types = [(getattr(x, 'repository_type', None) == 'built', x)
                      for x in source_repos]
        source_repos = source_repos.change_repos(
            [x[1] for x in repo_types if x[0]] +
            [x[1] for x in repo_types if not x[0]]
        )
    elif options.source_only:
        source_repos = source_repos.change_repos(
            x for x in source_repos
            if getattr(x, 'repository_type', None) == 'source')

    atoms = []
    for setname, pkgset in options.set:
        if pkgset is None:
            return 1
        l = list(pkgset)
        if not l:
            out.write("skipping set %s: set is empty, nothing to update" % setname)
        else:
            atoms.extend(l)

    for token in options.targets:
        try:
            a = parse_atom(token, source_repos.combined, livefs_repos, return_none=True)
        except parserestrict.ParseError as e:
            out.error(str(e))
            return 1
        if a is None:
            if token in config.pkgset:
                out.error(
                    'No package matches %r, but there is a set with '
                    'that name. Use -s to specify a set.' % (token,))
                return 2
            elif not options.ignore_failures:
                out.error('No matches for %r; ignoring it' % token)
            else:
                return -1
        else:
            atoms.append(a)

    if not atoms and not options.newuse:
        out.error('No targets specified; nothing to do')
        return 1

    atoms = stable_unique(atoms)

    if (not options.set or options.clean) and not options.oneshot:
        if world_set is None:
            err.write("Disable world updating via --oneshot, or fix your configuration")
            return 1

    if options.upgrade:
        resolver_kls = resolver.upgrade_resolver
    else:
        resolver_kls = resolver.min_install_resolver

    extra_kwargs = {}
    if options.empty:
        extra_kwargs['resolver_cls'] = resolver.empty_tree_merge_plan
    if options.debug:
        extra_kwargs['debug'] = True

    # XXX: This should recurse on deep
    if options.newuse:
        out.write(out.bold, ' * ', out.reset, 'Scanning for changed USE...')
        out.title('Scanning for changed USE...')
        for inst_pkg in installed_repos.itermatch(OrRestriction(*atoms)):
            src_pkgs = source_repos.match(inst_pkg.versioned_atom)
            if src_pkgs:
                src_pkg = max(src_pkgs)
                inst_iuse = set(use.lstrip("+-") for use in inst_pkg.iuse)
                src_iuse = set(use.lstrip("+-") for use in src_pkg.iuse)
                inst_flags = inst_iuse.intersection(inst_pkg.use)
                src_flags = src_iuse.intersection(src_pkg.use)
                if inst_flags.symmetric_difference(src_flags) or \
                   inst_pkg.iuse.symmetric_difference(src_pkg.iuse):
                    atoms.append(src_pkg.unversioned_atom)

#    left intentionally in place for ease of debugging.
#    from guppy import hpy
#    hp = hpy()
#    hp.setrelheap()

    resolver_inst = resolver_kls(
        installed_repos.repositories, source_repos.repositories,
        verify_vdb=options.deep, nodeps=options.nodeps,
        drop_cycles=options.ignore_cycles, force_replace=options.replace,
        process_built_depends=options.with_bdeps, **extra_kwargs)

    if options.preload_vdb_state:
        out.write(out.bold, ' * ', out.reset, 'Preloading vdb... ')
        vdb_time = time()
        resolver_inst.load_vdb_state()
        vdb_time = time() - vdb_time
    else:
        vdb_time = 0.0

    failures = []
    resolve_time = time()
    out.title('Resolving...')
    out.write(out.bold, ' * ', out.reset, 'Resolving...')
    ret = resolver_inst.add_atoms(atoms, finalize=True)
    while ret:
        out.error('resolution failed')
        restrict = ret[0][0]
        just_failures = reduce_to_failures(ret[1])
        display_failures(out, just_failures, debug=options.debug)
        failures.append(restrict)
        if not options.ignore_failures:
            break
        out.write("restarting resolution")
        atoms = [x for x in atoms if x != restrict]
        resolver_inst.reset()
        ret = resolver_inst.add_atoms(atoms, finalize=True)
    resolve_time = time() - resolve_time

    if options.debug:
        out.write(out.bold, " * ", out.reset, "resolution took %.2f seconds" % resolve_time)

    if failures:
        out.write()
        out.write('Failures encountered:')
        for restrict in failures:
            out.error("failed '%s'" % (restrict,))
            out.write('potentials:')
            match_count = 0
            for r in repo_utils.get_raw_repos(source_repos.repositories):
                l = r.match(restrict)
                if l:
                    out.write(
                        "repo %s: [ %s ]" % (r, ", ".join(str(x) for x in l)))
                    match_count += len(l)
            if not match_count:
                out.write("No matches found in %s" % (source_repos.repositories,))
            out.write()
            if not options.ignore_failures:
                return 1

    resolver_inst.free_caches()

    if options.clean:
        out.write(out.bold, ' * ', out.reset, 'Packages to be removed:')
        vset = set(installed_repos.combined)
        len_vset = len(vset)
        vset.difference_update(x.pkg for x in resolver_inst.state.iter_ops(True))
        wipes = sorted(x for x in vset if x.package_is_real)
        for x in wipes:
            out.write("Remove %s" % x)
        out.write()
        if wipes:
            out.write("removing %i packages of %i installed, %0.2f%%." %
                      (len(wipes), len_vset, 100*(len(wipes)/float(len_vset))))
        else:
            out.write("no packages to remove")
        if options.pretend:
            return 0
        if options.ask:
            if not formatter.ask("Do you wish to proceed?", default_answer=False):
                return 1
            out.write()
        repo_obs = observer.repo_observer(observer.formatter_output(out), not options.debug)
        do_unmerge(options, out, err, installed_repos.combined, wipes, world_set, repo_obs)
        return 0

    if options.debug:
        out.write()
        out.write(out.bold, ' * ', out.reset, 'debug: all ops')
        out.first_prefix.append(" ")
        plan_len = len(str(len(resolver_inst.state.plan)))
        for pos, op in enumerate(resolver_inst.state.plan):
            out.write(str(pos + 1).rjust(plan_len), ': ', str(op))
        out.first_prefix.pop()
        out.write(out.bold, ' * ', out.reset, 'debug: end all ops')
        out.write()

    changes = resolver_inst.state.ops(only_real=True)

    build_obs = observer.build_observer(observer.formatter_output(out), not options.debug)
    repo_obs = observer.repo_observer(observer.formatter_output(out), not options.debug)

    if options.debug:
        out.write(out.bold, " * ", out.reset, "running sanity checks")
        start_time = time()
    if not changes.run_sanity_checks(domain, build_obs):
        out.error("sanity checks failed.  please resolve them and try again.")
        return 1
    if options.debug:
        out.write(
            out.bold, " * ", out.reset,
            "finished sanity checks in %.2f seconds" % (time() - start_time))
        out.write()

    if options.ask or options.pretend:
        for op in changes:
            formatter.format(op)
        formatter.end()

    if vdb_time:
        out.write(out.bold, 'Took %.2f' % (vdb_time,), out.reset,
                  ' seconds to preload vdb state')
    if not changes:
        out.write("Nothing to merge.")
        return

    if options.pretend:
        if options.verbose:
            out.write(
                out.bold, ' * ', out.reset,
                "resolver plan required %i ops (%.2f seconds)\n" %
                (len(resolver_inst.state.plan), resolve_time))
        return

    if (options.ask and not formatter.ask("Would you like to merge these packages?")):
        return

    change_count = len(changes)

    # left in place for ease of debugging.
    cleanup = []
    try:
        for count, op in enumerate(changes):
            for func in cleanup:
                func()

            cleanup = []

            out.write("\nProcessing %i of %i: %s" % (count + 1, change_count, op.pkg.cpvstr))
            out.title("%i/%i: %s" % (count + 1, change_count, op.pkg.cpvstr))
            if op.desc != "remove":
                cleanup = [op.pkg.release_cached_data]

                if not options.fetchonly and options.debug:
                    out.write("Forcing a clean of workdir")

                pkg_ops = domain.pkg_operations(op.pkg, observer=build_obs)
                out.write("\n%i files required-" % len(op.pkg.fetchables))
                try:
                    ret = pkg_ops.run_if_supported("fetch", or_return=True)
                except IGNORED_EXCEPTIONS:
                    raise
                except Exception as e:
                    ret = e
                if ret is not True:
                    if ret is False:
                        ret = None
                    commandline.dump_error(out, ret, "\nfetching failed for %s" % (op.pkg.cpvstr,))
                    if not options.ignore_failures:
                        return 1
                    continue
                if options.fetchonly:
                    continue

                buildop = pkg_ops.run_if_supported("build", or_return=None)
                pkg = op.pkg
                if buildop is not None:
                    out.write("building %s" % (op.pkg.cpvstr,))
                    result = False
                    try:
                        result = buildop.finalize()
                    except format.errors as e:
                        out.error("caught exception building %s: % s" % (op.pkg.cpvstr, e))
                    else:
                        if result is False:
                            out.error("failed building %s" % (op.pkg.cpvstr,))
                    if result is False:
                        if not options.ignore_failures:
                            return 1
                        continue
                    pkg = result
                    cleanup.append(pkg.release_cached_data)
                    pkg_ops = domain.pkg_operations(pkg, observer=build_obs)
                    cleanup.append(buildop.cleanup)

                cleanup.append(partial(pkg_ops.run_if_supported, "cleanup"))
                pkg = pkg_ops.run_if_supported("localize", or_return=pkg)
                # wipe this to ensure we don't inadvertantly use it further down;
                # we aren't resetting it after localizing, so could have the wrong
                # set of ops.
                del pkg_ops

                out.write()
                if op.desc == "replace":
                    if op.old_pkg == pkg:
                        out.write(">>> Reinstalling %s" % (pkg.cpvstr))
                    else:
                        out.write(">>> Replacing %s with %s" % (
                            op.old_pkg.cpvstr, pkg.cpvstr))
                    i = domain.replace_pkg(op.old_pkg, pkg, repo_obs)
                    cleanup.append(op.old_pkg.release_cached_data)
                else:
                    out.write(">>> Installing %s" % (pkg.cpvstr,))
                    i = domain.install_pkg(pkg, repo_obs)

                # force this explicitly- can hold onto a helluva lot more
                # then we would like.
            else:
                out.write(">>> Removing %s" % op.pkg.cpvstr)
                i = domain.uninstall_pkg(op.pkg, repo_obs)
            try:
                ret = i.finish()
            except merge_errors.BlockModification as e:
                out.error("Failed to merge %s: %s" % (op.pkg, e))
                if not options.ignore_failures:
                    return 1
                continue

            # while this does get handled through each loop, wipe it now; we don't need
            # that data, thus we punt it now to keep memory down.
            # for safety sake, we let the next pass trigger a release also-
            # mainly to protect against any code following triggering reloads
            # basically, be protective

            if world_set is not None:
                if op.desc == "remove":
                    out.write('>>> Removing %s from world file' % op.pkg.cpvstr)
                    removal_pkg = slotatom_if_slotted(source_repos.combined, op.pkg.versioned_atom)
                    update_worldset(world_set, removal_pkg, remove=True)
                elif not options.oneshot and any(x.match(op.pkg) for x in atoms):
                    if not options.upgrade:
                        out.write('>>> Adding %s to world file' % op.pkg.cpvstr)
                        add_pkg = slotatom_if_slotted(source_repos.combined, op.pkg.versioned_atom)
                        update_worldset(world_set, add_pkg)


#    again... left in place for ease of debugging.
#    except KeyboardInterrupt:
#        import pdb;pdb.set_trace()
#    else:
#        import pdb;pdb.set_trace()
    finally:
        pass

    # the final run from the loop above doesn't invoke cleanups;
    # we could ignore it, but better to run it to ensure nothing is inadvertantly
    # held on the way out of this function.
    # makes heappy analysis easier if we're careful about it.
    for func in cleanup:
        func()

    # and wipe the reference to the functions to allow things to fall out of
    # memory.
    cleanup = []

    out.write("finished")
    return 0
