"""Wrappers for interacting with ``git``"""

import asyncio
import logging
import os
import tempfile
from typing import List

import git
import yaml

from . import utils


logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class GitHandlerFailure(Exception):
    """Something went wrong interacting with git"""


class GitHandlerBase():
    """GitPython abstraction

    We have to work with three git repositories, the local checkout,
    the project primary repository and a working repository. The
    latter may be a fork or may be the same as the primary.


    Arguments:
      repo: GitPython Repo object (created by subclasses)
      dry_run: Don't push anything to remote
      home: string occurring in remote url marking primary project repo
      fork: string occurring in remote url marking forked repo
      allow_dirty: don't bail out if repo is dirty
    """
    def __init__(self, repo: git.Repo,
                 dry_run: bool,
                 home='bioconda/bioconda-recipes',
                 fork=None,
                 allow_dirty=False) -> None:
        #: GitPython Repo object representing our repository
        self.repo: git.Repo = repo
        if not allow_dirty and self.repo.is_dirty():
            raise RuntimeError("Repository is in dirty state. Bailing out")
        #: Dry-Run mode - don't push or commit anything
        self.dry_run = dry_run
        #: Remote pointing to primary project repo
        self.home_remote = self.get_remote(home)
        if fork is not None:
            #: Remote to pull from
            self.fork_remote = self.get_remote(fork)
        else:
            self.fork_remote = self.home_remote

        #: Semaphore for things that mess with workding directory
        self.lock_working_dir = asyncio.Semaphore(1)

    def close(self):
        """Release resources allocated"""
        self.repo.close()

    def __str__(self):
        def get_name(remote):
            url = next(remote.urls)
            return url[url.rfind('/', 0, url.rfind('/'))+1:]
        name = get_name(self.home_remote)
        if self.fork_remote != self.home_remote:
            name = f"{name} <- {get_name(self.fork_remote)}"
        return f"{self.__class__.__name__}({name})"

    def get_remote(self, desc: str):
        """Finds first remote containing **desc** in one of its URLs"""
        # try if desc is the name
        if desc in [r.name for r in self.repo.remotes]:
            return self.repo.remotes[desc]

        # perhaps it's an URL. If so, first apply insteadOf config
        with self.repo.config_reader() as reader:
            for section in reader.sections():
                if section.startswith("url "):
                    new = section.lstrip("url ").strip('"')
                    try:
                        old = reader.get(section, 'insteadOf')
                        desc = desc.replace(old, new)
                    except KeyError:
                        pass
        # now try if any remote matches the url
        remotes = [r for r in self.repo.remotes
                   if any(filter(lambda x: desc in x, r.urls))]

        if not remotes:
            raise KeyError(f"No remote matching '{desc}' found")
        if len(remotes) > 1:
            logger.warning("Multiple remotes found. Using first")

        return remotes[0]

    async def branch_is_current(self, branch, path: str, master="master") -> bool:
        """Checks if **branch** has the most recent commit modifying **path**
        as compared to **master**"""
        proc = await asyncio.create_subprocess_exec(
            'git', 'log', '-1', '--oneline', '--decorate',
            f'{master}...{branch.name}', '--', path,
            stdout=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        return branch.name in stdout.decode('ascii')

    def delete_local_branch(self, branch) -> None:
        """Deletes **branch** locally"""
        git.Reference.delete(self.repo, branch)

    def delete_remote_branch(self, branch_name: str) -> None:
        """Deletes **branch** on fork remote"""
        if not self.dry_run:
            logger.info("Deleting branch %s", branch_name)
            self.fork_remote.push(":" + branch_name)
        else:
            logger.info("Would delete branch %s", branch_name)

    def get_local_branch(self, branch_name: str):
        """Finds local branch named **branch_name**"""
        if branch_name in self.repo.branches:
            return self.repo.branches[branch_name]
        try:
            return self.repo.commit(branch_name)
        except git.BadName:
            pass
        return None

    def get_remote_branch(self, branch_name: str, try_fetch=False):
        """Finds fork remote branch named **branch_name**"""
        if branch_name in self.fork_remote.refs:
            return self.fork_remote.refs[branch_name]
        if not try_fetch:
            return None
        for depth in (0, 50, 200):
            try:
                if depth > 0:
                    self.fork_remote.fetch(depth=depth)
                remote_refs = self.fork_remote.fetch(branch_name, depth=depth)
                break
            except git.GitCommandError:
                pass
        else:
            logger.info("Failed to fetch %s", branch_name)
            return None
        for remote_ref in remote_refs:
            if remote_ref.remote_ref_path == branch_name:
                return remote_ref.ref

    def get_latest_master(self):
        return self.home_remote.fetch('master')[0].commit

    def read_from_branch(self, branch, file_name: str) -> str:
        """Reads contents of file **file_name** from git branch **branch**"""
        abs_file_name = os.path.abspath(file_name)
        abs_repo_root = os.path.abspath(self.repo.working_dir)
        if not abs_file_name.startswith(abs_repo_root):
            raise RuntimeError(
                f"File {abs_file_name} not inside {abs_repo_root}"
            )
        rel_file_name = abs_file_name[len(abs_repo_root):].lstrip("/")
        commit = getattr(branch, 'commit', branch)
        return (commit.tree / rel_file_name).data_stream.read().decode("utf-8")

    def create_local_branch(self, branch_name: str, remote_branch: str = None):
        """Creates local branch from remote **branch_name**"""
        if remote_branch is None:
            remote_branch = self.get_remote_branch(branch_name, try_fetch=True)
        else:
            remote_branch = self.get_remote_branch(remote_branch, try_fetch=True)
        if remote_branch is None:
            return None
        self.repo.create_head(branch_name, remote_branch)
        return self.get_local_branch(branch_name)

    def get_merge_base(self, ref=None, other=None):
        """Determines the merge base for **other** and **ref**

        See git merge-base. Returns the commit at which **ref** split
        from **other** and from which point on changes would be
        merged.

        Args:
          ref: One of the two tips for which a merge base is sought.
               Defaults to the currently checked out HEAD. This is the
               second argument to ``git merge-base``.
          other: One of the two tips for which a merge base is sought.
               Defaults to ``origin/master`` (``home_remote``). This is
               the first argument to ``git merge-base``.

        Returns:
          The first merge base for the two references provided if found.
          May return `None` if no merge base was found. This may for
          example be the case if branches were deleted or if the
          repository is shallow and the merge base commit not available.
        """
        if not ref:
            ref = self.repo.active_branch.commit
        if not other:
            other = self.home_remote.refs.master
        for depth in (0, 50, 200):
            if depth:
                self.fork_remote.fetch(ref, depth=depth)
                self.home_remote.fetch('master', depth=depth)
            merge_bases = self.repo.merge_base(other, ref)
            if merge_bases:
                break
            logger.debug("No merge base found for %s and master at depth %i", ref, depth)
        else:
            logger.error("No merge base found for %s and master", ref)
            return None   # FIXME: This should raise
        if len(merge_bases) > 1:
            logger.error("Multiple merge bases found for %s and master: %s",
                         ref, merge_bases)
        return merge_bases[0]

    def list_changed_files(self, ref=None, other=None):
        """Lists files that would be added/modified by merge of **other** into **ref**

        See also `get_merge_base()`.

        Args:
          ref: Defaults to ``HEAD`` (active branch), one of the tips compared
          other: Defaults to ``origin/master``, other tip compared

        Returns:
          Generator over modified or created (**not deleted**) files.
        """
        if not ref:
            ref = self.repo.active_branch.commit
        merge_base = self.get_merge_base(ref, other)
        for diffobj in merge_base.diff(ref):
            if not diffobj.deleted_file:
                yield diffobj.b_path

    def prepare_branch(self, branch_name: str) -> None:
        """Checks out **branch_name**, creating it from home remote master if needed"""
        if branch_name not in self.repo.heads:
            logger.info("Creating new branch %s", branch_name)
            from_commit = self.get_latest_master()
            self.repo.create_head(branch_name, from_commit)
        logger.info("Checking out branch %s", branch_name)
        branch = self.repo.heads[branch_name]
        branch.checkout()

    def commit_and_push_changes(self, files: List[str], branch_name: str,
                                msg: str, sign=False) -> bool:
        """Create recipe commit and pushes to upstream remote

        Returns:
          Boolean indicating whether there were changes committed
        """
        if branch_name is None:
            branch_name = self.repo.active_branch.name
        self.repo.index.add(files)
        if not self.repo.index.diff("HEAD"):
            return False
        if sign:
            # Gitpyhon does not support signing, so we use the command line client here
            self.repo.index.write()
            self.repo.git.commit('-S', '-m', msg)
        else:
            self.repo.index.commit(msg)
        if not self.dry_run:
            logger.info("Pushing branch %s", branch_name)
            res = self.fork_remote.push(branch_name)
            failed = res[0].flags & ~(git.PushInfo.FAST_FORWARD | git.PushInfo.NEW_HEAD)
            if failed:
                logger.error("Failed to push branch %s: %s", branch_name, res[0].summary)
                raise GitHandlerFailure(res[0].summary)
        else:
            logger.info("Would push branch %s", branch_name)
        return True

    def set_user(self, user: str, email: str = None, key: str = None) -> None:
        with self.repo.config_writer() as writer:
            writer.set_value("user", "name", user)
            email = email or f"{user}@users.noreply.github.com"
            writer.set_value("user", "email", email)
            if key is not None:
                writer.set_value("user", "signingkey", key)


class BiocondaRepoMixin(GitHandlerBase):
    """Githandler with logic specific to Bioconda Repo"""

    #: location of recipes folder within repo
    recipes_folder = "recipes"

    #: location of configuration file within repo
    config_file = "config.yml"

    def get_changed_recipes(self, ref=None, other=None, files=None):
        """Returns list of modified recipes

        Args:
          ref: See `get_merge_base`. Defaults to HEAD
          other: See `get_merge_base`. Defaults to origin/master
          files: List of files to consider. Defaults to ``meta.yaml``
                 and ``build.sh``
        Result:
          List of unique recipe folders with changes. Path is from repo
          root (e.g. ``recipes/blast``). Recipes outside of
          ``recipes_folder`` are ignored.
        """
        if files is None:
            files = ['meta.yaml', 'build.sh']
        changed = set()
        for path in self.list_changed_files(ref, other):
            if not path.startswith(self.recipes_folder):
                continue  # skip things outside the recipes folder
            for fname in files:
                if os.path.basename(path) == fname:
                    changed.add(os.path.dirname(path))
        return list(changed)

    def get_blacklisted(self, ref=None):
        """Get blacklisted recipes as of **ref**

        Args:
          ref: Name of branch or commit (HEAD~1 is allowed), defaults to
               currently checked out branch
        Returns:
          `set` of blacklisted recipes (full path to repo root)
        """
        if ref is None:
            branch = self.repo.active_branch
        elif isinstance(ref, str):
            branch = self.get_local_branch(ref)
        else:
            branch = ref
        config_data = self.read_from_branch(branch, self.config_file)
        config = yaml.safe_load(config_data)
        blacklists = config['blacklists']
        blacklisted = set()
        for blacklist in blacklists:
            blacklist_data = self.read_from_branch(branch, blacklist)
            for line in blacklist_data.splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                recipe_folder, _, _ = line.partition(" #")
                blacklisted.add(recipe_folder.strip())
        return blacklisted

    def get_unblacklisted(self, ref=None, other=None):
        """Get recipes unblacklisted by a merge of **ref** into **other**

        Args:
          ref: Branch or commit or reference, defaults to current branch
          other: Same as **ref**, defaults to ``origin/master``

        Returns:
          `set` of unblacklisted recipes (full path to repo root)
        """
        merge_base = self.get_merge_base(ref, other)
        orig_blacklist = self.get_blacklisted(merge_base)
        cur_blacklist = self.get_blacklisted(ref)
        return orig_blacklist.difference(cur_blacklist)

    def get_recipes_to_build(self, ref=None, other=None):
        """Returns `list` of recipes to build for merge of **ref** into **other**

        This includes all recipes returned by `get_changed_recipes` and
        all newly unblacklisted, extant recipes within `recipes_folder`

        Returns:
          `list` of recipes that should be built
        """
        tobuild = set(self.get_changed_recipes(ref, other))
        tobuild.update([recipe
                        for recipe in self.get_unblacklisted(ref, other)
                        if recipe.startswith(self.recipes_folder)
                        and os.path.exists(recipe)])
        return list(tobuild)


class GitHandler(GitHandlerBase):
    """GitHandler for working with a pre-existing local checkout of bioconda-recipes

    Restores the branch active when created upon calling `close()`.
    """
    def __init__(self, folder: str=".",
                 dry_run=False,
                 home='bioconda/bioconda-recipes',
                 fork=None,
                 allow_dirty=True,
                 depth=1) -> None:
        if os.path.exists(folder):
            repo = git.Repo(folder, search_parent_directories=True)
        else:
            try:
                os.mkdir(folder)
                logger.error("cloning %s into %s", home, folder)
                repo = git.Repo.clone_from(home, folder, depth=depth)
            except git.GitCommandError:
                os.rmdir(folder)
                raise
        super().__init__(repo, dry_run, home, fork, allow_dirty)

        #: Branch to restore after running
        self.prev_active_branch = self.repo.active_branch
        self.master = self.get_local_branch("master")

    def checkout_master(self):
        """Check out master branch (original branch restored by `close()`)"""
        logger.warning("Checking out master")
        self.master.checkout()
        logger.info("Updating master to latest project master")
        self.home_remote.pull("master")
        logger.info("Updating and pruning remotes")
        self.home_remote.fetch(prune=True)
        self.fork_remote.fetch(prune=True)

    def close(self) -> None:
        """Release resources allocated"""
        logger.warning("Switching back to %s", self.prev_active_branch.name)
        self.prev_active_branch.checkout()
        super().close()


class TempGitHandler(GitHandlerBase):
    """GitHandler for working with temporary working directories created on the fly
    """
    def __init__(self,
                 username: str = None,
                 password: str = None,
                 url_format="https://{userpass}github.com/{user}/{repo}.git",
                 home_user="bioconda",
                 home_repo="bioconda-recipes",
                 fork_user=None,
                 fork_repo=None,
                 dry_run=False) -> None:
        userpass = ""
        if password is not None and username is None:
            username = "x-access-token"
        if username is not None:
            userpass = username
            if password is not None:
                userpass += ":" + password
            userpass += "@"

        self.tempdir = tempfile.TemporaryDirectory()

        def censor(string):
            if password is None:
                return string
            return string.replace(password, "******")

        home_url = url_format.format(userpass=userpass,
                                     user=home_user, repo=home_repo)
        logger.info("Cloning %s to %s", censor(home_url), self.tempdir.name)
        repo = git.Repo.clone_from(home_url, self.tempdir.name,  depth=1)

        if fork_repo is not None:
            fork_url = url_format.format(userpass=userpass, user=fork_user, repo=fork_repo)
            if fork_url != home_url:
                logger.warning("Adding remote fork %s", censor(fork_url))
                fork_remote = repo.create_remote("fork", fork_url)
        else:
            fork_url = None
        logger.info("Finished setting up repo in %s", self.tempdir)
        super().__init__(repo, dry_run, home_url, fork_url)

    def close(self) -> None:
        """Remove temporary clone and cleanup resources"""
        super().close()
        logger.info("Removing repo from %s", self.tempdir.name)
        self.tempdir.cleanup()


class BiocondaRepo(GitHandler, BiocondaRepoMixin):
    pass

class TempBiocondaRepo(TempGitHandler, BiocondaRepoMixin):
    pass
