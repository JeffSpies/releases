import re
import sys
from functools import partial
from distutils.version import LooseVersion

from docutils import nodes, utils


def _log(txt, config):
    """
    Log debug output if debug setting is on.

    Intended to be partial'd w/ config at top of functions. Meh.
    """
    if config.releases_debug:
        sys.stderr.write(str(txt) + "\n")
        sys.stderr.flush()


# Issue type list (keys) + color values
issue_types = {
    'bug': 'A04040',
    'feature': '40A056',
    'support': '4070A0',
}

def issue_nodelist(name, link=None):
    which = '[<span style="color: #%s;">%s</span>]' % (
        issue_types[name], name.capitalize()
    )
    signifier = [nodes.raw(text=which, format='html')]
    hyperlink = [nodes.inline(text=" "), link] if link else []
    trail = [] if link else [nodes.inline(text=" ")]
    return signifier + hyperlink + [nodes.inline(text=":")] + trail


release_line_re = re.compile(r'\((\d+\.\d+)\+\)') # e.g. '(1.2+)'

def issues_role(name, rawtext, text, lineno, inliner, options={}, content=[]):
    """
    Use: :issue|bug|feature|support:`ticket_number`

    When invoked as :issue:, turns into just a "#NN" hyperlink to
    `releases_issue_uri`.

    When invoked otherwise, turns into "[Type] <#NN hyperlink>: ".

    May give a 'ticket number' of '<number> backported' to indicate a
    backported feature or support ticket. This extra info will be stripped out
    prior to parsing. May also give 'major' in the same vein, implying the bug
    was a major bug released in a feature release. May give a 'ticket number'
    of ``-`` or ``0`` to generate no hyperlink.
    """
    # Old-style 'just the issue link' behavior
    issue_no, _, ported = utils.unescape(text).partition(' ')
    # Lol @ access back to Sphinx
    config = inliner.document.settings.env.app.config
    if issue_no not in ('-', '0'):
        ref = config.releases_issue_uri % issue_no
        link = nodes.reference(rawtext, '#' + issue_no, refuri=ref, **options)
    else:
        link = None
        issue_no = None # So it doesn't gum up dupe detection later
    # Additional 'new-style changelog' stuff
    if name in issue_types:
        nodelist = issue_nodelist(name, link)
        line = None
        # Sanity check
        if ported not in ('backported', 'major', ''):
            match = release_line_re.match(ported)
            if not match:
                raise ValueError("Gave unknown issue metadata '%s' for issue no. %s" % (ported, issue_no))
            else:
                line = match.groups()[0]
        # Create temporary node w/ data & final nodes to publish
        node = issue(
            number=issue_no,
            type_=name,
            nodelist=nodelist,
            backported=(ported == 'backported'),
            major=(ported == 'major'),
            line=line,
        )
        return [node], []
    # Return old style info for 'issue' for older changelog entries
    else:
        return [link], []


def release_nodes(text, slug, date, config):
    # Doesn't seem possible to do this "cleanly" (i.e. just say "make me a
    # title and give it these HTML attributes during render time) so...fuckit.
    # We were already doing fully raw elements elsewhere anyway. And who cares
    # about a PDF of a changelog? :x
    link = '<a class="reference external" href="{0}">{1}</a>'.format(
        config.releases_release_uri % slug,
        text,
    )
    datespan = ''
    if date:
        datespan = ' <span style="font-size: 75%%;">{0}</span>'.format(date)
    header = '<h2 style="margin-bottom: 0.3em;">{0}{1}</h2>'.format(link, datespan)
    return nodes.section('',
        nodes.raw(rawtext='', text=header, format='html'),
        ids=[text]
    )


year_arg_re = re.compile(r'^(.+?)\s*(?<!\x00)<(.*?)>$', re.DOTALL)

def release_role(name, rawtext, text, lineno, inliner, options={}, content=[]):
    """
    Invoked as :release:`N.N.N <YYYY-MM-DD>`.

    Turns into useful release header + link to GH tree for the tag.
    """
    # Make sure year has been specified
    match = year_arg_re.match(text)
    if not match:
        msg = inliner.reporter.error("Must specify release date!")
        return [inliner.problematic(rawtext, rawtext, msg)], [msg]
    number, date = match.group(1), match.group(2)
    # Lol @ access back to Sphinx
    config = inliner.document.settings.env.app.config
    nodelist = [release_nodes(number, number, date, config)]
    # Return intermediate node
    node = release(number=number, date=date, nodelist=nodelist)
    return [node], []


class issue(nodes.Element):
    @property
    def type(self):
        return self['type_']

    @property
    def backported(self):
        return self.get('backported', False)

    @property
    def major(self):
        return self.get('major', False)

    @property
    def number(self):
        return self.get('number', None)

    @property
    def line(self):
        return self.get('line', None)

    def __repr__(self):
        flag = ""
        if self.backported:
            flag = "backported"
        elif self.major:
            flag = "major"
        elif self.line:
            flag = self.line + "+"
        if flag:
            flag = " (%s)" % flag
        return "<%s #%s%s>" % (self.type, self.number, flag)


class release(nodes.Element):
    @property
    def number(self):
        return self['number']

    def __repr__(self):
        return "<release %s>" % self.number


def get_line(obj):
    # 1.2.7 -> 1.2
    return '.'.join(obj.number.split('.')[:-1])

def construct_releases(entries, app):
    log = partial(_log, config=app.config)
    # Walk from back to front, consuming entries & copying them into
    # per-release buckets as releases are encountered. Store releases in order.
    releases = []
    lines = {'unreleased_bugfix': [], 'unreleased_feature': []}
    # Also keep a master hash of issues by number to detect duplicates & assist
    # in explicitly defined release lists.
    issues = {}
    for obj in reversed(entries):
        # The 'actual' intermediate object we want to focus on is wrapped first
        # in a LI, then a P.
        focus, rest = obj[0][0], obj[0][1:]
        log(repr(focus))
        # Releases 'eat' the entries in their line's list and get added to the
        # final data structure. They also inform new release-line 'buffers'.
        # Release lines' "rest" should be empty or a comma-separated list of
        # issue numbers.
        if isinstance(focus, release):
            line = get_line(focus)
            log("release for line %r" % line)
            # Check for explicitly listed issues first
            explicit = None
            if rest:
                explicit = [x.strip() for x in rest[0].split(',')]
            # Do those by themselves since they override all other logic
            if explicit:
                log("Explicit issues requested: %r" % (explicit,))
                # First scan global issue dict, dying if not found
                missing = [i for i in explicit if i not in issues]
                if missing:
                    raise ValueError("Couldn't find issue(s) #%s in the changelog!" % (', '.join(missing)))
                # Obtain objects from global list
                entries = [issues[i] for i in explicit]
                # Create release
                log("entries in this release: %r" % (entries,))
                releases.append({
                    'obj': focus,
                    'entries': entries,
                })
                # Introspect entries to determine which buckets they should get
                # removed from
                for obj in entries:
                    if obj.type == 'bug':
                        # Major bugfix: remove from unreleased_feature
                        if obj.major:
                            log("Removing #%s from unreleased" % obj.number)
                            lines['unreleased_feature'].remove(obj)
                        # Regular bugfix: remove from bucket for this release's
                        # line + unreleased_bugfix
                        else:
                            if obj in lines['unreleased_bugfix']:
                                log("Removing #%s from unreleased" % obj.number)
                                lines['unreleased_bugfix'].remove(obj)
                            if obj in lines[line]:
                                log("Removing #%s from %s" % (obj.number, line))
                                lines[line].remove(obj)
                    # Regular feature/support: remove from unreleased_feature
                    # Backported feature/support: remove from bucket for this
                    # release's line (if applicable) + unreleased_feature
                    else:
                        log("Removing #%s from unreleased" % obj.number)
                        lines['unreleased_feature'].remove(obj)
                        if obj in lines.get(line, []):
                            lines[line].remove(obj)

            # Implicit behavior otherwise
            else:
                # New release line/branch detected. Create it & dump unreleased
                # features.
                if line not in lines:
                    log("not seen prior, making feature release")
                    lines[line] = []
                    entries = [
                        x
                        for x in lines['unreleased_feature']
                        if x.type in ('feature', 'support') or x.major
                    ]
                    releases.append({
                        'obj': focus,
                        'entries': entries
                    })
                    lines['unreleased_feature'] = []
                # Existing line -> empty out its bucket into new release.
                # Skip 'major' bugs as those "belong" to the next release (and will
                # also be in 'unreleased_feature' - so safe to nuke the entire
                # line)
                else:
                    log("pre-existing, making bugfix release")
                    entries = [x for x in lines[line] if not x.major]
                    log("entries in this release: %r" % (entries,))
                    releases.append({
                        'obj': focus,
                        'entries': entries,
                    })
                    lines[line] = []
                    # Clean out the items we just released from
                    # 'unreleased_bugfix'.  (Can't nuke it because there might
                    # be some unreleased bugs for other release lines.)
                    for x in entries:
                        if x in lines['unreleased_bugfix']:
                            lines['unreleased_bugfix'].remove(x)
        # Entries get copied into release line buckets as follows:
        # * Features and support go into 'unreleased_feature' for use in new
        # feature releases.
        # * Bugfixes go into all release lines (so they can be printed in >1
        # bugfix release as appropriate) as well as 'unreleased_bugfix' (so
        # they can be displayed prior to release'). Caveats include bugs marked
        # 'major' (they go into unreleased_feature instead) or with 'N.N+'
        # (meaning they only go into release line buckets for that release and
        # up.)
        # * Support/feature entries marked as 'backported' go into all
        # release lines as well, on the assumption that they were released to
        # all active branches.
        # * The 'rest' variable (which here is the bug description, vitally
        # important!) is preserved by stuffing it into the focus (issue)
        # object.
        else:
            # Handle rare-but-valid non-issue-attached line items, which are
            # always bugs. (They are their own description.)
            if not isinstance(focus, issue):
                log("Found line item w/ no real issue object, creating bug")
                focus = issue(
                    type_='bug',
                    nodelist=issue_nodelist('bug'),
                    description=[focus]
                )
            else:
                focus.attributes['description'] = rest
            # Add to global list or die trying
            if focus.number and focus.number in issues:
                raise ValueError("You seem to have defined issue #%s twice! Please double check." % focus.number)
            else:
                issues[focus.number] = focus
            if focus.type == 'bug':
                # Major bugs go into unreleased_feature
                if focus.major:
                    lines['unreleased_feature'].append(focus)
                    log("Adding to unreleased_feature")
                # Regular bugs go into per-line buckets ('major' bugs do
                # not) as well as unreleased_bugfix. Adjust for bugs with a
                # 'line' (minimum line no.) attribute.
                else:
                    bug_lines = [x for x in lines if x != 'unreleased_feature']
                    if focus.line:
                        bug_lines = [
                            x for x in bug_lines
                            if (
                                x != 'unreleased_bugfix'
                                and LooseVersion(x) >= LooseVersion(focus.line)
                            )
                        ] + ['unreleased_bugfix']
                    for line in bug_lines:
                        log("Adding to %r" % line)
                        lines[line].append(focus)
            else:
                # Backported feature/support items go into all lines, including
                # both 'unreleased' lists
                if focus.backported:
                    for line in lines:
                        log("Adding to release line %r" % line)
                        lines[line].append(focus)
                # Non-backported feature/support items go into feature releases
                # only.
                else:
                    log("Adding to unreleased_feature")
                    lines['unreleased_feature'].append(focus)

    # Entries not yet released get special 'release' entries (that lack an
    # actual release object).
    for which in ('bugfix', 'feature'):
        nodelist = [release_nodes(
            "Next %s release" % which,
            'master',
            None,
            app.config
        )]
        line = 'unreleased_%s' % which
        log("Creating '%s' faux-release with %r" % (line, lines[line]))
        releases.append({
            'obj': release(number=line, date=None, nodelist=nodelist),
            'entries': lines[line]
        })
    return releases


def construct_nodes(releases):
    result = []
    # Reverse the list again so the final display is newest on top
    for d in reversed(releases):
        if not d['entries']:
            continue
        obj = d['obj']
        entries = []
        for entry in d['entries']:
            # Use nodes.Node.deepcopy to deepcopy the description
            # nodes.  If this is not done, multiple references to the same
            # object (e.g. a reference object in the description of #649, which
            # is then copied into 2 different release lists) will end up in the
            # doctree, which makes subsequent parse steps very angry (index()
            # errors).
            desc = list(map(lambda x: x.deepcopy(), entry['description']))
            # Additionally, expand any other issue roles found in the
            # description paragraph - sometimes we refer to related issues
            # inline. (They can't be left as issue() objects at render time
            # since that's undefined.)
            for i, node in enumerate(desc[:]): # Copy to avoid self-mutation during loop
                if isinstance(node, issue):
                    desc[i:i+1] = node['nodelist']
            # Tack on to end of this entry's own nodelist (which is the link +
            # etc)
            entries.append(
                nodes.list_item('',
                    nodes.paragraph('', '', *entry['nodelist'] + desc)
                )
            )
        # Entry list
        list_ = nodes.bullet_list('', *entries)
        # Insert list into release nodelist (as it's a section)
        obj['nodelist'][0].append(list_)
        # Release header
        header = nodes.paragraph('', '', *obj['nodelist'])
        result.extend(header)
    return result


def generate_changelog(app, doctree):
    # This seems to be the cleanest way to tell what a not-fully-parsed
    # document's 'name' is. Also lol @ not fully implementing dict protocol.
    source = doctree[0]
    if 'changelog' not in source.get('names', []):
        return
    # Second item inside main document is the 'modern' changelog bullet-list
    # object, whose children are the nodes we care about.
    changelog = source.children.pop(1)
    # Walk + parse into release mapping
    releases = construct_releases(changelog.children, app)
    # Construct new set of nodes to replace the old, and we're done
    source[1:1] = construct_nodes(releases)


def setup(app):
    # Issue base URI setting: releases_issue_uri
    # E.g. 'https://github.com/fabric/fabric/issues/'
    app.add_config_value(name='releases_issue_uri', default=None,
        rebuild='html')
    # Release-tag base URI setting: releases_release_uri
    # E.g. 'https://github.com/fabric/fabric/tree/'
    app.add_config_value(name='releases_release_uri', default=None,
        rebuild='html')
    # Debug output
    app.add_config_value(name='releases_debug', default=False, rebuild='html')
    # Register intermediate roles
    for x in list(issue_types) + ['issue']:
        app.add_role(x, issues_role)
    app.add_role('release', release_role)
    # Hook in our changelog transmutation at appropriate step
    app.connect('doctree-read', generate_changelog)
