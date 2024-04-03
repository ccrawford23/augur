#SPDX-License-Identifier: MIT

import logging
from celery import group, chain
import sqlalchemy as s

from augur.application.db.lib import execute_sql, fetchall_data_from_sql_text, get_session, get_repo_by_repo_git, get_repo_by_repo_id, remove_working_commits_by_repo_id_and_hashes, get_working_commits_by_repo_id

from augur.tasks.git.util.facade_worker.facade_worker.utilitymethods import trim_commits
from augur.tasks.git.util.facade_worker.facade_worker.utilitymethods import get_absolute_repo_path, get_parent_commits_set, get_existing_commits_set
from augur.tasks.git.util.facade_worker.facade_worker.analyzecommit import analyze_commit
from augur.tasks.git.util.facade_worker.facade_worker.utilitymethods import get_repo_commit_count, update_facade_scheduling_fields, get_facade_weight_with_commit_count, facade_bulk_insert_commits
from augur.tasks.git.util.facade_worker.facade_worker.rebuildcache import fill_empty_affiliations, invalidate_caches, nuke_affiliations, rebuild_unknown_affiliation_and_web_caches
from augur.tasks.git.util.facade_worker.facade_worker.postanalysiscleanup import git_repo_cleanup


from augur.tasks.github.facade_github.tasks import *
from augur.tasks.git.util.facade_worker.facade_worker.config import FacadeHelper
from augur.tasks.util.collection_state import CollectionState
from augur.tasks.util.collection_util import get_collection_status_repo_git_from_filter
from augur.tasks.git.util.facade_worker.facade_worker.repofetch import GitCloneError, git_repo_initialize, git_repo_updates



from augur.tasks.init.celery_app import celery_app as celery
from augur.tasks.init.celery_app import AugurFacadeRepoCollectionTask


from augur.application.db.models import Repo, CollectionStatus

from augur.tasks.git.dependency_tasks.tasks import process_dependency_metrics
from augur.tasks.git.dependency_libyear_tasks.tasks import process_libyear_dependency_metrics
from augur.tasks.git.scc_value_tasks.tasks import process_scc_value_metrics

from augur.tasks.github.util.github_task_session import *


#define an error callback for chains in facade collection so facade doesn't make the program crash
#if it does.
@celery.task
def facade_error_handler(request,exc,traceback):

    logger = logging.getLogger(facade_error_handler.__name__)

    logger.error(f"Task {request.id} raised exception: {exc}! \n {traceback}")

    print(f"chain: {request.chain}")
    #Make sure any further execution of tasks dependent on this one stops.
    try:
        #Replace the tasks queued ahead of this one in a chain with None.
        request.chain = None
    except AttributeError:
        pass #Task is not part of a chain. Normal so don't log.
    except Exception as e:
        logger.error(f"Could not mutate request chain! \n Error: {e}")


#Predefine facade collection with tasks
@celery.task(base=AugurFacadeRepoCollectionTask)
def facade_analysis_init_facade_task(repo_git):

    logger = logging.getLogger(facade_analysis_init_facade_task.__name__)
    facade_helper = FacadeHelper(logger)

    facade_helper.update_status('Running analysis')
    facade_helper.log_activity('Info',f"Beginning analysis.")


@celery.task(base=AugurFacadeRepoCollectionTask)
def trim_commits_facade_task(repo_git):

    logger = logging.getLogger(trim_commits_facade_task.__name__)

    facade_helper = FacadeHelper(logger)

    repo = get_repo_by_repo_git(repo_git)

    repo_id = repo.repo_id

    def update_analysis_log(repos_id,status):

    # Log a repo's analysis status

        log_message = s.sql.text("""INSERT INTO analysis_log (repos_id,status)
            VALUES (:repo_id,:status)""").bindparams(repo_id=repos_id,status=status)

        try:
            execute_sql(log_message)
        except:
            pass


        facade_helper.inc_repos_processed()
        update_analysis_log(repo_id,"Beginning analysis.")
        # First we check to see if the previous analysis didn't complete

        working_commits = get_working_commits_by_repo_id(repo_id)

        # If there's a commit still there, the previous run was interrupted and
        # the commit data may be incomplete. It should be trimmed, just in case.
        commits_to_trim = [commit['working_commit'] for commit in working_commits]
        
        trim_commits(facade_helper,repo_id,commits_to_trim)
        # Start the main analysis

        update_analysis_log(repo_id,'Collecting data')
        logger.info(f"Got past repo {repo_id}")

@celery.task(base=AugurFacadeRepoCollectionTask)
def trim_commits_post_analysis_facade_task(repo_git):

    logger = logging.getLogger(trim_commits_post_analysis_facade_task.__name__)
    
    facade_helper = FacadeHelper(logger)

    repo = repo = get_repo_by_repo_git(repo_git)
    repo_id = repo.repo_id

    start_date = facade_helper.get_setting('start_date')
    def update_analysis_log(repos_id,status):

        # Log a repo's analysis status

        log_message = s.sql.text("""INSERT INTO analysis_log (repos_id,status)
            VALUES (:repo_id,:status)""").bindparams(repo_id=repos_id,status=status)

        
        execute_sql(log_message)
    
    logger.info(f"Generating sequence for repo {repo_id}")

    repo = get_repo_by_repo_git(repo_git)

    #Get the huge list of commits to process.
    absoulte_path = get_absolute_repo_path(facade_helper.repo_base_directory, repo.repo_id, repo.repo_path,repo.repo_name)
    repo_loc = (f"{absoulte_path}/.git")
    # Grab the parents of HEAD

    parent_commits = get_parent_commits_set(repo_loc, start_date)

    # Grab the existing commits from the database
    existing_commits = get_existing_commits_set(repo_id)

    # Find missing commits and add them

    missing_commits = parent_commits - existing_commits

    facade_helper.log_activity('Debug',f"Commits missing from repo {repo_id}: {len(missing_commits)}")
    
    # Find commits which are out of the analysis range

    trimmed_commits = existing_commits - parent_commits

    update_analysis_log(repo_id,'Data collection complete')

    update_analysis_log(repo_id,'Beginning to trim commits')

    facade_helper.log_activity('Debug',f"Commits to be trimmed from repo {repo_id}: {len(trimmed_commits)}")

    #for commit in trimmed_commits:
    trim_commits(facade_helper,repo_id,trimmed_commits)
    

    update_analysis_log(repo_id,'Commit trimming complete')

    update_analysis_log(repo_id,'Complete')
    


@celery.task
def facade_analysis_end_facade_task():

    logger = logging.getLogger(facade_analysis_end_facade_task.__name__)
    facade_helper = FacadeHelper(logger)
    facade_helper.log_activity('Info','Running analysis (complete)')



@celery.task
def facade_start_contrib_analysis_task():

    logger = logging.getLogger(facade_start_contrib_analysis_task.__name__)
    facade_helper = FacadeHelper(logger)
    facade_helper.update_status('Updating Contributors')
    facade_helper.log_activity('Info', 'Updating Contributors with commits')


#enable celery multithreading
@celery.task(base=AugurFacadeRepoCollectionTask)
def analyze_commits_in_parallel(repo_git, multithreaded: bool)-> None:
    """Take a large list of commit data to analyze and store in the database. Meant to be run in parallel with other instances of this task.
    """

    #create new session for celery thread.
    logger = logging.getLogger(analyze_commits_in_parallel.__name__)
    facade_helper = FacadeHelper(logger)

    repo = get_repo_by_repo_git(repo_git)
    repo_id = repo.repo_id

    start_date = facade_helper.get_setting('start_date')

    logger.info(f"Generating sequence for repo {repo_id}")
    
    repo = get_repo_by_repo_id(repo_id)

    #Get the huge list of commits to process.
    absoulte_path = get_absolute_repo_path(facade_helper.repo_base_directory, repo.repo_id, repo.repo_path, repo.repo_name)
    repo_loc = (f"{absoulte_path}/.git")
    # Grab the parents of HEAD

    parent_commits = get_parent_commits_set(repo_loc, start_date)

    # Grab the existing commits from the database
    existing_commits = get_existing_commits_set(repo_id)

    # Find missing commits and add them
    missing_commits = parent_commits - existing_commits

    facade_helper.log_activity('Debug',f"Commits missing from repo {repo_id}: {len(missing_commits)}")

    
    if not len(missing_commits) or repo_id is None:
        #session.log_activity('Info','Type of missing_commits: %s' % type(missing_commits))
        return
    
    queue = list(missing_commits)

    logger.info(f"Got to analysis!")
    absoulte_path = get_absolute_repo_path(facade_helper.repo_base_directory, repo.repo_id, repo.repo_path,repo.repo_name)
    repo_loc = (f"{absoulte_path}/.git")

    pendingCommitRecordsToInsert = []

    with get_session() as session:

        for count, commitTuple in enumerate(queue):
            quarterQueue = int(len(queue) / 4)

            if quarterQueue == 0:
                quarterQueue = 1 # prevent division by zero with integer math

            #Log progress when another quarter of the queue has been processed
            if (count + 1) % quarterQueue == 0:
                logger.info(f"Progress through current analysis queue is {(count / len(queue)) * 100}%")

            #logger.info(f"Got to analysis!")
            commitRecords = analyze_commit(logger, repo_id, repo_loc, commitTuple)
            #logger.debug(commitRecord)
            if len(commitRecords):
                pendingCommitRecordsToInsert.extend(commitRecords)
                if len(pendingCommitRecordsToInsert) >= 1000:
                    facade_bulk_insert_commits(logger, session,pendingCommitRecordsToInsert)
                    pendingCommitRecordsToInsert = []

        
        facade_bulk_insert_commits(logger, session,pendingCommitRecordsToInsert)

    # Remove the working commit.
    remove_working_commits_by_repo_id_and_hashes(repo_id, queue)

    logger.info("Analysis complete")
    return

@celery.task
def nuke_affiliations_facade_task():

    logger = logging.getLogger(nuke_affiliations_facade_task.__name__)
    
    facade_helper = FacadeHelper(logger)
    nuke_affiliations(facade_helper)

@celery.task
def fill_empty_affiliations_facade_task():

    logger = logging.getLogger(fill_empty_affiliations_facade_task.__name__)
    facade_helper = FacadeHelper(logger)
    fill_empty_affiliations(facade_helper)

@celery.task
def invalidate_caches_facade_task():

    logger = logging.getLogger(invalidate_caches_facade_task.__name__)

    facade_helper = FacadeHelper(logger)
    invalidate_caches(facade_helper)

@celery.task
def rebuild_unknown_affiliation_and_web_caches_facade_task():

    logger = logging.getLogger(rebuild_unknown_affiliation_and_web_caches_facade_task.__name__)
    
    facade_helper = FacadeHelper(logger)
    rebuild_unknown_affiliation_and_web_caches(facade_helper)


@celery.task
def git_repo_cleanup_facade_task(repo_git):

    logger = logging.getLogger(git_repo_cleanup_facade_task.__name__)

    facade_helper = FacadeHelper(logger)
    with get_session() as session:
        git_repo_cleanup(facade_helper, session, repo_git)

# retry this task indefinitely every 5 minutes if it errors. Since the only way it gets scheduled is by itself, so if it stops running no more clones will happen till the instance is restarted
@celery.task(autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=300, retry_jitter=True, max_retries=None)
def clone_repos():

    logger = logging.getLogger(clone_repos.__name__)
    
    is_pending = CollectionStatus.facade_status == CollectionState.PENDING.value

    facade_helper = FacadeHelper(logger)

    with get_session() as session:

        # process up to 1000 repos at a time
        repo_git_identifiers = get_collection_status_repo_git_from_filter(session, is_pending, 999999)
        for repo_git in repo_git_identifiers:
            # set repo to intializing
            repo = get_repo_by_repo_git(repo_git)
            repoStatus = repo.collection_status[0]
            setattr(repoStatus,"facade_status", CollectionState.INITIALIZING.value)
            session.commit()

            # clone repo
            try:
                git_repo_initialize(facade_helper, session, repo_git)
                session.commit()

                # get the commit count
                commit_count = get_repo_commit_count(logger, facade_helper, session, repo_git)
                facade_weight = get_facade_weight_with_commit_count(session, repo_git, commit_count)

                update_facade_scheduling_fields(session, repo_git, facade_weight, commit_count)

                # set repo to update
                setattr(repoStatus,"facade_status", CollectionState.UPDATE.value)
                session.commit()
            except GitCloneError:
                # continue to next repo, since we can't calculate 
                # commit_count or weight without the repo cloned
                setattr(repoStatus,"facade_status", CollectionState.FAILED_CLONE.value)
                session.commit()
            except Exception as e:
                logger.error(f"Ran into unexpected issue when cloning repositories \n Error: {e}")
                # set repo to error
                setattr(repoStatus,"facade_status", CollectionState.ERROR.value)
                session.commit()

            clone_repos.si().apply_async(countdown=60*5)


#@celery.task(bind=True)
#def check_for_repo_updates_facade_task(self, repo_git):
#
#    engine = self.app.engine
#
#    logger = logging.getLogger(check_for_repo_updates_facade_task.__name__)
#
#    facade_helper = FacadeHelper(logger)
#        check_for_repo_updates(session, repo_git)

@celery.task(base=AugurFacadeRepoCollectionTask, bind=True)
def git_update_commit_count_weight(self, repo_git):

    engine = self.app.engine
    logger = logging.getLogger(git_update_commit_count_weight.__name__)
    
    # Change facade session to take in engine
    facade_helper = FacadeHelper(logger)

    with get_session() as session:

        commit_count = get_repo_commit_count(logger, facade_helper, session, repo_git)
        facade_weight = get_facade_weight_with_commit_count(session, repo_git, commit_count)

        update_facade_scheduling_fields(session, repo_git, facade_weight, commit_count)


@celery.task(base=AugurFacadeRepoCollectionTask)
def git_repo_updates_facade_task(repo_git):

    logger = logging.getLogger(git_repo_updates_facade_task.__name__)

    facade_helper = FacadeHelper(logger)

    with get_session() as session:

        git_repo_updates(facade_helper, session, repo_git)


def generate_analysis_sequence(logger,repo_git, facade_helper):
    """Run the analysis by looping over all active repos. For each repo, we retrieve
    the list of commits which lead to HEAD. If any are missing from the database,
    they are filled in. Then we check to see if any commits in the database are
    not in the list of parents, and prune them out.

    We also keep track of the last commit to be processed, so that if the analysis
    is interrupted (possibly leading to partial data in the database for the
    commit being analyzed at the time) we can recover.
    """

    analysis_sequence = []

    #repo_list = s.sql.text("""SELECT repo_id,repo_group_id,repo_path,repo_name FROM repo WHERE repo_git=:value""").bindparams(value=repo_git)
    #repos = fetchall_data_from_sql_text(repo_list)

    start_date = facade_helper.get_setting('start_date')

    #repo_ids = [repo['repo_id'] for repo in repos]

    #repo_id = repo_ids.pop(0)

    analysis_sequence.append(facade_analysis_init_facade_task.si(repo_git))

    analysis_sequence.append(trim_commits_facade_task.si(repo_git))

    analysis_sequence.append(analyze_commits_in_parallel.si(repo_git,True))

    analysis_sequence.append(trim_commits_post_analysis_facade_task.si(repo_git))

    
    analysis_sequence.append(facade_analysis_end_facade_task.si())
    
    logger.info(f"Analysis sequence: {analysis_sequence}")
    return analysis_sequence



def generate_contributor_sequence(logger,repo_git, session):
    
    contributor_sequence = []
    #all_repo_ids = []
    repo_id = None
        
    #contributor_sequence.append(facade_start_contrib_analysis_task.si())
    repo = get_repo_by_repo_git(repo_git)
    repo_id = repo.repo_id

    #pdb.set_trace()
    #breakpoint()
    #for repo in all_repos:
    #    contributor_sequence.append(insert_facade_contributors.si(repo['repo_id']))
    #all_repo_ids = [repo['repo_id'] for repo in all_repos]

    #contrib_group = create_grouped_task_load(dataList=all_repo_ids,task=insert_facade_contributors)#group(contributor_sequence)
    #contrib_group.link_error(facade_error_handler.s())
    #return contrib_group#chain(facade_start_contrib_analysis_task.si(), contrib_group)
    return insert_facade_contributors.si(repo_id)


def facade_phase(repo_git):
    logger = logging.getLogger(facade_phase.__name__)
    logger.info("Generating facade sequence")
    facade_helper = FacadeHelper(logger)
    #Get the repo_id
    #repo_list = s.sql.text("""SELECT repo_id,repo_group_id,repo_path,repo_name FROM repo WHERE repo_git=:value""").bindparams(value=repo_git)
    #repos = fetchall_data_from_sql_text(repo_list)

    start_date = facade_helper.get_setting('start_date')

    #repo_ids = [repo['repo_id'] for repo in repos]

    #repo_id = repo_ids.pop(0)

    #Get the collectionStatus
    #query = session.query(CollectionStatus).filter(CollectionStatus.repo_id == repo_id)

    #status = execute_session_query(query,'one')
    
    # Figure out what we need to do
    limited_run = facade_helper.limited_run
    run_analysis = facade_helper.run_analysis
    pull_repos = facade_helper.pull_repos
    #force_analysis = session.force_analysis
    run_facade_contributors = facade_helper.run_facade_contributors

    facade_sequence = []
    facade_core_collection = []

    if not limited_run or (limited_run and pull_repos):
        facade_core_collection.append(git_repo_updates_facade_task.si(repo_git))
    
    facade_core_collection.append(git_update_commit_count_weight.si(repo_git))

    #Generate commit analysis task order.
    if not limited_run or (limited_run and run_analysis):
        facade_core_collection.extend(generate_analysis_sequence(logger,repo_git,facade_helper))

    #Generate contributor analysis task group.
    if not limited_run or (limited_run and run_facade_contributors):
        facade_core_collection.append(generate_contributor_sequence(logger,repo_git,facade_helper))


    #These tasks need repos to be cloned by facade before they can work.
    facade_sequence.append(
        group(
            chain(*facade_core_collection),
            process_dependency_metrics.si(repo_git),
            process_libyear_dependency_metrics.si(repo_git),
            process_scc_value_metrics.si(repo_git)
        )
    )

    logger.info(f"Facade sequence: {facade_sequence}")
    return chain(*facade_sequence)

def generate_non_repo_domain_facade_tasks(logger):
    logger.info("Generating facade sequence")
    facade_helper = FacadeHelper(logger)
        
    # Figure out what we need to do
    limited_run = facade_helper.limited_run
    delete_marked_repos = facade_helper.delete_marked_repos
    pull_repos = facade_helper.pull_repos
    # clone_repos = facade_helper.clone_repos
    check_updates = facade_helper.check_updates
    # force_updates = facade_helper.force_updates
    run_analysis = facade_helper.run_analysis
    # force_analysis = facade_helper.force_analysis
    nuke_stored_affiliations = facade_helper.nuke_stored_affiliations
    fix_affiliations = facade_helper.fix_affiliations
    force_invalidate_caches = facade_helper.force_invalidate_caches
    rebuild_caches = facade_helper.rebuild_caches
    #if abs((datetime.datetime.strptime(session.cfg.get_setting('aliases_processed')[:-3], 
        # '%Y-%m-%d %I:%M:%S.%f') - datetime.datetime.now()).total_seconds()) // 3600 > int(session.cfg.get_setting(
        #   'update_frequency')) else 0
    force_invalidate_caches = facade_helper.force_invalidate_caches
    create_xlsx_summary_files = facade_helper.create_xlsx_summary_files
    multithreaded = facade_helper.multithreaded

    facade_sequence = []

    if nuke_stored_affiliations:
        #facade_sequence.append(nuke_affiliations_facade_task.si().on_error(facade_error_handler.s()))#nuke_affiliations(session.cfg)
        logger.info("Nuke stored affiliations is deprecated.")
        # deprecated because the UI component of facade where affiliations would be 
        # nuked upon change no longer exists, and this information can easily be derived 
        # from queries and materialized views in the current version of Augur.
        # This method is also a major performance bottleneck with little value.

    #logger.info(session.cfg)
    if not limited_run or (limited_run and fix_affiliations):
        #facade_sequence.append(fill_empty_affiliations_facade_task.si().on_error(facade_error_handler.s()))#fill_empty_affiliations(session)
        logger.info("Fill empty affiliations is deprecated.")
        # deprecated because the UI component of facade where affiliations would need 
        # to be fixed upon change no longer exists, and this information can easily be derived 
        # from queries and materialized views in the current version of Augur.
        # This method is also a major performance bottleneck with little value.

    if force_invalidate_caches:
        facade_sequence.append(invalidate_caches_facade_task.si().on_error(facade_error_handler.s()))#invalidate_caches(session.cfg)

    if not limited_run or (limited_run and rebuild_caches):
        facade_sequence.append(rebuild_unknown_affiliation_and_web_caches_facade_task.si().on_error(facade_error_handler.s()))#rebuild_unknown_affiliation_and_web_caches(session.cfg)
    
    return facade_sequence
