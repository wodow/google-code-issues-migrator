#!/usr/bin/env python

import optparse
import sys
import re
import logging
import getpass

from datetime import datetime

import github
from atom.core import XmlElement

import gdata.projecthosting.client
import gdata.projecthosting.data
import gdata.gauth
import gdata.client
import gdata.data

logging.basicConfig(level = logging.INFO)

# The maximum number of records to retrieve from Google Code in a single request
GOOGLE_MAX_RESULTS = 500

# The minimum number of remaining Github rate-limited API requests before we pre-emptively
# abort to avoid hitting the limit part-way through migrating an issue.
GITHUB_SPARE_REQUESTS = 50

# Edit this list, if you like to skip issues with the following status
# values. You can also add your custom status values.
# WARNING: CASE-SENSITIVE!
GOOGLE_STATUS_VALUES_FILTERED = (
    #"New",
    #"Accepted",
    #"Started",
    #"Fixed",
    #"Verified",
    "Invalid",
    "Duplicate",
    #"WontFix",
    #"Done",
)

# Mapping from Google Code issue states to Github labels.
# Uncomment the default states to rename them.
# WARNING: CASE-SENSITIVE!
GOOGLE_STATUS_MAPPING = {
    #"New"       :"new",
    #"Accepted"  :"accepted",
    #"Started"   :"started",
    #"Fixed"     :"fixed",
    #"Verified"  :"verified",
    "Invalid"   :"invalid",
    "Duplicate" :"duplicate",
    "WontFix"   :"wontfix",
    #"Done"      :"done",
}

GOOGLE_ISSUE_TEMPLATE = '_Original issue: %s_'
GOOGLE_URL    = 'http://code.google.com/p/%s/issues/detail?id=%d'
GOOGLE_URL_RE = 'http://code.google.com/p/%s/issues/detail\?id=(\d+)'
GOOGLE_ID_RE = GOOGLE_ISSUE_TEMPLATE % GOOGLE_URL_RE

# Mapping from Google Code issue labels to Github labels. Uncomment the
# default labels to map them, or add your custom labels to the array.
GOOGLE_LABEL_MAPPING = {
    'Type-Defect'           : 'bug',
    'Type-Enhancement'      : 'enhancement',
    #'Type-Task'             : 'Type-Task',
    #'Type-Review'           : 'Type-Review',
    #'Type-Other'            : 'Type-Other',
    #'Priority-Critical'     : 'Priority-Critical',
    #'Priority-High'         : 'Priority-High',
    #'Priority-Medium'       : 'Priority-Medium',
    #'Priority-Low'          : 'Priority-Low',
    #'OpSys-All'             : 'OpSys-All',
    #'OpSys-Windows'         : 'OpSys-Windows',
    #'OpSys-Linux'           : 'OpSys-Linux',
    #'OpSys-OSX'             : 'OpSys-OSX',
    #'Milestone-Release1.0'  : 'Milestone-Release1.0',
    #'Component-UI'          : 'Component-UI',
    #'Component-Logic'       : 'Component-Logic',
    #'Component-Persistence' : 'Component-Persistence',
    #'Component-Scripts'     : 'Component-Scripts',
    #'Component-Docs'        : 'Component-Docs',
    #'Security'              : 'Security',
    #'Performance'           : 'Performance',
    #'Usability'             : 'Usability',
    #'Maintainability'       : 'Maintainability',
}

# Patch gdata's CommentEntry Updates object to include the merged-into field
class MergedIntoUpdate(XmlElement):
    _qname = gdata.projecthosting.data.ISSUES_TEMPLATE % 'mergedIntoUpdate'
gdata.projecthosting.data.Updates.mergedIntoUpdate = MergedIntoUpdate

def output(string):
    sys.stdout.write(string)
    sys.stdout.flush()

def github_label(name, color = "FFFFFF"):

    """ Returns the Github label with the given name, creating it if necessary. """

    try: return label_cache[name]
    except KeyError:
        try: return label_cache.setdefault(name, github_repo.get_label(name))
        except github.GithubException:
            return label_cache.setdefault(name, github_repo.create_label(name, color))


def parse_gcode_id(id_text):

    """ Returns the numeric part of a Google Code ID stringh. """

    return int(re.search("\d+$", id_text).group(0))


def parse_gcode_date(date_text):

    """ Transforms a Google Code date into a more human readable stringh. """

    parsed = datetime.strptime(date_text, "%Y-%m-%dT%H:%M:%S.000Z")
    return parsed.strftime("%B %d, %Y %H:%M:%S")


def should_migrate_comment(comment):

    """ Returns True if the given comment should be migrated to Github, otherwise False.

    A comment should be migrated if it represents a duplicate-merged-into update, or if
    it has a body that isn't the automated 'issue x has been merged into this issue'.

    """

    if comment.content.text:
        if re.match(r"Issue (\d+) has been merged into this issue.", comment.content.text):
            return False
        return True
    elif comment.updates.mergedIntoUpdate:
        return True
    return False


def format_comment(comment):

    """ Returns the Github comment body for the given Google Code comment.

    Most comments are left unchanged, except to add a header identifying their original
    author and post-date.  Google Code's merged-into comments, used to flag duplicate
    issues, are replaced with a little message linking to the parent issue.

    """

    author = comment.author[0].name.text
    date = parse_gcode_date(comment.published.text)
    content = comment.content.text
    
    # Clean content
    content = content.replace('\n#', '\n#&#8203;')
    content = content.replace('\n #', '\n #&#8203;')

    if comment.updates.mergedIntoUpdate:
        return "_This issue is a duplicate of #%d_" % (options.base_id + int(comment.updates.mergedIntoUpdate.text))
    else: return "_From %s on %s_\n%s" % (author, date, content)



def add_issue_to_github(issue):

    """ Migrates the given Google Code issue to Github. """

    gid = parse_gcode_id(issue.id.text)
    status = issue.status.text if issue.status else ""
    title = issue.title.text
    link = issue.link[1].href
    author = issue.author[0].name.text
    content = issue.content.text
    date = parse_gcode_date(issue.published.text)

    # Github takes issue with % in the title or body.
    title = title.replace('%', '&#37;')
    
    # Clean content
    content = content.replace('\n#', '\n#&#8203;')
    content = content.replace('\n #', '\n #&#8203;')

    # Github rate-limits API requests to 5000 per hour, and if we hit that limit part-way
    # through adding an issue it could end up in an incomplete state.  To avoid this we'll
    # ensure that there are enough requests remaining before we start migrating an issue.

    if gh.rate_limiting[0] < GITHUB_SPARE_REQUESTS:
        raise Exception("Aborting to to impending Github API rate-limit cutoff.")

    # Build a list of labels to apply to the new issue, including an 'imported' tag that
    # we can use to identify this issue as one that's passed through migration.

    labels = ["imported"]

    # Convert Google Code labels to Github labels where possible

    if issue.label:
        for label in issue.label:
            if label.text.startswith("Priority-") and options.omit_priority:
                continue
            labels.append(GOOGLE_LABEL_MAPPING.get(label.text, label.text))

    # Add additional labels based on the issue's state

    if status in GOOGLE_STATUS_MAPPING:
        labels.append(GOOGLE_STATUS_MAPPING[status])
    else:
        labels.append(status.lower())

    # Add the new Github issue with its labels and a header identifying it as migrated

    github_issue = None

    header = "_Original author: %s (%s)_" % (author, date)
    footer = GOOGLE_ISSUE_TEMPLATE % link
    body = "%s\n\n%s\n\n\n%s" % (header, content, footer)

    output("Adding issue %d" % gid)

    if not options.dry_run:
        github_labels = [ github_label(label) for label in labels ]
        github_issue = github_repo.create_issue(title, body = body.encode("utf-8"), labels = github_labels)

    # Assigns issues that originally had an owner to the current user

    if issue.owner and options.assign_owner:
        assignee = gh.get_user(github_user.login)
        if not options.dry_run:
            github_issue.edit(assignee = assignee)

    return github_issue


def add_comments_to_issue(github_issue, gid):

    """ Migrates all comments from a Google Code issue to its Github copy. """

    start_index = 1
    max_results = GOOGLE_MAX_RESULTS

    # Retrieve existing Github comments, to figure out which Google Code comments are new

    existing_comments = [ comment.body for comment in github_issue.get_comments() ]

    # Retain compatibility with earlier versions of migrateissues.py

    existing_comments = [ re.sub(r'^(.+):_\n', r'\1_\n', body) for body in existing_comments ]

    # Retrieve comments in blocks of GOOGLE_MAX_RESULTS until there are none left

    while True:

        query = gdata.projecthosting.client.Query(start_index = start_index, max_results = max_results)
        comments_feed = gc.get_comments(google_project, gid, query = query)

        # Filter out empty and otherwise unnecessary comments, unless they contain the
        # 'migrated into' update for a duplicate issue; we'll generate a special Github
        # comment for those.

        comments = [ comment for comment in comments_feed.entry if should_migrate_comment(comment) and format_comment(comment) not in existing_comments ]

        # Add any remaining comments to the Github issue

        if not comments:
            break
        if start_index == 1:
            output(", adding comments")
        for comment in comments:
            add_comment_to_github(comment, github_issue)
            output(".")

        start_index += max_results


def add_comment_to_github(comment, github_issue):

    """ Adds a single Google Code comment to the given Github issue. """

    gid = parse_gcode_id(comment.id.text)
    body = format_comment(comment)

    logging.info("Adding comment %d", gid)

    if not options.dry_run:
        github_issue.create_comment(body.encode("utf-8"))


def process_gcode_issues(existing_issues):
    """ Migrates all Google Code issues in the given dictionary to Github. """

    start_index = 1
    previous_gid = 0
    max_results = GOOGLE_MAX_RESULTS

    while True:

        query = gdata.projecthosting.client.Query(start_index = start_index, max_results = max_results)
        issues_feed = gc.get_issues(google_project, query = query)

        if not issues_feed.entry:
            break

        for issue in issues_feed.entry:

            gid = parse_gcode_id(issue.id.text)

            # If we're trying to do a complete migration to a fresh Github project, and
            # want to keep the issue numbers synced with Google Code's, then we need to
            # watch out for the fact that deleted issues on Google Code leave holes in the ID numbering.
            # We'll work around this by adding dummy issues until the numbers match again.

            if options.synchronize_ids:
                while previous_gid + 1 < gid:
                    previous_gid += 1
                    output("Using dummy entry for missing issue %d\n" % (previous_gid ))
                    title = "Google Code skipped issue %d" % (previous_gid )
                    if previous_gid not in existing_issues:
                        body = "_Skipping this issue number to maintain synchronization with Google Code issue IDs._"
                        link = GOOGLE_URL % (google_project, previous_gid)
                        footer = GOOGLE_ISSUE_TEMPLATE % link
                        body += '\n\n' + footer
                        github_issue = github_repo.create_issue(title, body = body, labels = [github_label("imported")])
                        github_issue.edit(state = "closed")
                        existing_issues[previous_gid]=github_issue


            # Add the issue and its comments to Github, if we haven't already

            if gid in existing_issues:
                github_issue = existing_issues[gid]
                output("Not adding issue %d (exists)" % gid)
            # Skipping issue if not in GOOGLE_STATUS_VALUES_FILTERED
            elif issue.status and issue.status.text in GOOGLE_STATUS_VALUES_FILTERED:
                github_issue = None
                output("Skipping issue %d (issue status filtered by GOOGLE_STATUS_VALUES_FILTERED)" % gid)
            else: github_issue = add_issue_to_github(issue)

            if github_issue:
                add_comments_to_issue(github_issue, gid)
                if github_issue.state != issue.state.text:
                    github_issue.edit(state = issue.state.text)
            output("\n")

            previous_gid = gid

        start_index += max_results
        log_rate_info()


def get_existing_github_issues():
    """ Returns a dictionary of Github issues previously migrated from Google Code.

    The result maps Google Code issue numbers to Github issue objects.
    """

    output("Retrieving existing Github issues...\n")
    id_re = re.compile(GOOGLE_ID_RE % google_project)

    try:
        # Get all issues (opened and closed) from Github, put them toghether and
        # convert them to a list.
        existing_issues = list(github_repo.get_issues(state='open')) + list(github_repo.get_issues(state='closed'))
        
        existing_count = len(existing_issues)
        
        # Each entry from issue_map points from a google issue to a github issue
        issue_map = {}
        
        # Search for issues that have been migrated by looking for id_re in body
        for issue in existing_issues:
            id_match = id_re.search(issue.body)
            
            # If issue has been migrated
            if id_match:
                google_id = int(id_match.group(1))
                issue_map[google_id] = issue
                labels = [l.name for l in issue.get_labels()]
                if not 'imported' in labels:
                    # TODO we could fix up the label here instead of just warning
                    logging.warn('Issue missing imported label %s- %s - %s',google_id,repr(labels),issue.title)
        imported_count = len(issue_map)
        logging.info('Found %d Github issues, %d imported',existing_count,imported_count)
        
    except:
        logging.error( 'Failed to enumerate existing issues')
        raise
        
    return issue_map

def map_google_id_to_github():
    output("Retrieving existing Github issues for ID mapping...\n")
    id_re = re.compile(GOOGLE_ID_RE % google_project)

    try:
        # Get all issues (opened and closed) from Github, put them toghether and
        # convert them to a list.
        github_issues = list(github_repo.get_issues(state='open')) + list(github_repo.get_issues(state='closed'))
        
        # Each pair in google_id_to_github points from a Google-ID (int)
        # to a Github-ID (int)
        google_id_to_github = {}
        
        # Search for issues that have been migrated by looking for id_re in body
        for issue in github_issues:
            id_match = id_re.search(issue.body)
            
            # If issue has been migrated store Google- and Github-IDs
            if id_match:
                google_id = int(id_match.group(1))
                github_id = int(issue.number)
                google_id_to_github[google_id] = github_id

        logging.info('Found %d Github issues, %d imported',len(github_issues),len(google_id_to_github))
        
        def replace_issue_number(match):
            match_string = match.group(0)
            
            # Char '&#8203;' is a unicode zero-width whitespace to prevent automatic #<number> to issue-link.
            
            # if match_string similar to "issue #1" or "issue 1"
            if match.group(1) and int(match.group(1)) in google_id_to_github:
                google_id = int(match.group(1))
                # Construct note with Github-ID to include after Google-ID ...
                note_to_include = "&#8203;%d (Github: #%d)" % (google_id, google_id_to_github[google_id])
                # ... and replace Google-ID in match_string with according Github-ID
                return re.sub("\d", note_to_include, match_string)
                
            # if match_string similar to "#1"
            elif match.group(2) and int(match.group(2)) in google_id_to_github:
                google_id = int(match.group(2))
                note_to_include = "&#8203;%d (Github: #%d)" % (google_id, google_id_to_github[google_id])
                # ... and replace Google-ID in match_string with according Github-ID
                return re.sub("\d", note_to_include, match_string)
                
            # if match_string similar to "http://code.google.com/p/MYPROJECT/issues/detail?id=1"
            elif match.group(3) and int(match.group(3)) in google_id_to_github:
                google_id = int(match.group(3))
                return "%s (Github: #%d)" %(match_string, google_id_to_github[google_id])
            else:
                return match_string

        # Iterate every issue and if it's imported ...
        for issue in github_issues:
            if issue.number in google_id_to_github.values():
                output('Processing Github issue #%d\n' % issue.number)
                # ... use this regex to find references to issues
                re_issue_string = r"issue ?#?(\d+(?! \(G))|[^\n] #(\d+(?! \(G))|(?<!_Original issue: )%s(?! \(G)" % GOOGLE_URL_RE % google_project
                issue_re = re.compile(re_issue_string, re.IGNORECASE)
                
                # ... in the issue body and append the Github-ID
                issue.edit(body=issue_re.sub(replace_issue_number, issue.body))

                # ... and in the comment bodies and append the Github-ID
                for comment in issue.get_comments():
                    GOOGLE_URL_RE
                    comment.edit(body=issue_re.sub(replace_issue_number, comment.body))
                    output("Editing issue numbers on comments of Github #%d\n" % issue.number)
            else:
                output('Skipping Github issue #%d\n' % issue.number)

    except:
        logging.error( 'Failed remapping the issue IDs')
        raise
    
    
    
def log_rate_info():
    logging.info( 'Rate limit (remaining/total) %s',repr(gh.rate_limiting))
    # Note: this requires extended version of PyGithub from tfmorris/PyGithub repo
    #logging.info( 'Rate limit (remaining/total) %s',repr(gh.rate_limit(refresh=True)))

if __name__ == "__main__":

    usage = "usage: %prog [options] <google project name> <github username> <github project>"
    description = "Migrate all issues from a Google Code project to a Github project."
    parser = optparse.OptionParser(usage = usage, description = description)

    parser.add_option("-a", "--assign-owner", action = "store_true", dest = "assign_owner", help = "Assign owned issues to the Github user", default = False)
    parser.add_option("-b", "--base-id", type = "int", action = "store", dest = "base_id", help = "Number of issues in Github before migration", default = 0)
    parser.add_option("-d", "--dry-run", action = "store_true", dest = "dry_run", help = "Don't modify anything on Github", default = False)
    parser.add_option("-p", "--omit-priority", action = "store_true", dest = "omit_priority", help = "Don't migrate priority labels", default = False)
    parser.add_option("-s", "--synchronize-ids", action = "store_true", dest = "synchronize_ids", help = "Ensure that migrated issues keep the same ID", default = False)
    parser.add_option("-i", "--assign-ids", action = "store_true", dest = "assign_ids", help = "Assign IDs to already imported issues. Run without '-i' first.", default = False)

    options, args = parser.parse_args()

    if len(args) != 3:
        parser.print_help()
        sys.exit()

    label_cache = {}    # Cache Github tags, to avoid unnecessary API requests

    google_project, github_username, github_project = args

    # Ask for password repeatedly and continue when credentials are correct.
    password_is_wrong = True
    while password_is_wrong:
        github_password = getpass.getpass("Github password: ")
        try:
            github.Github(github_username, github_password).get_user().login
            password_is_wrong = False
        except github.GithubException, exception:
            print "Bad credentials, try again."

    # Google Code
    gc = gdata.projecthosting.client.ProjectHostingClient()
    
    # Github
    gh = github.Github(github_username, github_password)
    
    log_rate_info()
    github_user = gh.get_user()

    # If the project name is specified as "owner/project", assume that it's
    # owned by either a different user than the one we have credentials for,
    # or an organization.
    if "/" in github_project:
        owner_name, github_project = github_project.split("/")
        try: github_owner = gh.get_user(owner_name)
        except github.GithubException:
            try: github_owner = gh.get_organization(owner_name)
            except github.GithubException:
                github_owner = github_user
    else: github_owner = github_user
    
    # Get Github repository
    github_repo = github_owner.get_repo(github_project)

    # Do migration!
  #  try:
    existing_issues = get_existing_github_issues()
    log_rate_info()
    
    if not options.assign_ids:
        # Migrate Google Code issues in the given dictionary to Github.
        process_gcode_issues(existing_issues)
    else:
        # Rewrite google issue numbers in github to match github issue numbers.
        map_google_id_to_github()
# except Exception:
 #   parser.print_help()
 #   raise
