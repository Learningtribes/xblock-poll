# pylint: disable=too-many-lines
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 McKinsey Academy
#
# Authors:
#          Jonathan Piacenti <jonathan@opencraft.com>
#
# This software's license gives you freedom; you can copy, convey,
# propagate, redistribute and/or modify this program under the terms of
# the GNU Affero General Public License (AGPL) as published by the Free
# Software Foundation (FSF), either version 3 of the License, or (at your
# option) any later version of the AGPL published by the FSF.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program in a file in the toplevel directory called
# "AGPLv3".  If not, see <http://www.gnu.org/licenses/>.
#
from abc import ABCMeta, abstractmethod
from collections import OrderedDict
import functools
import json
import time

from markdown import markdown
import pkg_resources
from webob import Response

from django import utils
from xblock.core import XBlock
from xblock.fields import Scope, String, Dict, List, Boolean, Integer
from xblock.fragment import Fragment
from xblockutils.publish_event import PublishEventMixin
from xblockutils.resources import ResourceLoader
from xblockutils.settings import XBlockWithSettingsMixin, ThemableXBlockMixin
from .utils import _, DummyTranslationService

try:
    # pylint: disable=import-error
    from django.conf import settings
    from api_manager.models import GroupProfile
    HAS_GROUP_PROFILE = True
except ImportError:
    HAS_GROUP_PROFILE = False

try:
    # pylint: disable=import-error
    from static_replace import replace_static_urls
    HAS_STATIC_REPLACE = True
except ImportError:
    HAS_STATIC_REPLACE = False


class ResourceMixin(XBlockWithSettingsMixin, ThemableXBlockMixin):
    loader = ResourceLoader(__name__)

    block_settings_key = 'poll'
    default_theme_config = {
        'package': 'poll',
        'locations': ["public/css/themes/lms.css"]
    }

    @staticmethod
    def resource_string(path):
        """Handy helper for getting resources from our kit."""
        data = pkg_resources.resource_string(__name__, path)
        return data.decode("utf8")

    @property
    def i18n_service(self):
        """ Obtains translation service """
        return self.runtime.service(self, "i18n") or DummyTranslationService()

    def get_translation_content(self):
        try:
            return self.resource_string('public/js/translations/{lang}/textjs.js'.format(
                lang=utils.translation.get_language(),
            ))
        except IOError:
            return self.resource_string('public/js/translations/en/textjs.js')

    def create_fragment(self, context, template, css, js, js_init):
        frag = Fragment()
        frag.add_content(self.loader.render_django_template(
            template,
            context=context,
            i18n_service=self.i18n_service,
        ))
        frag.add_javascript_url(
            self.runtime.local_resource_url(self, 'public/js/vendor/handlebars.js')
        )

        frag.add_css(self.resource_string(css))

        frag.add_javascript(self.get_translation_content())
        frag.add_javascript(self.resource_string('public/js/poll_common.js'))
        frag.add_javascript(self.resource_string(js))
        frag.initialize_js(js_init)
        self.include_theme_files(frag)
        return frag


class CSVExportMixin(object):
    """
    Allows Poll or Surveys XBlocks to support CSV downloads of all users'
    details per block.
    """
    active_export_task_id = String(
        # The UUID of the celery AsyncResult for the most recent export,
        # IF we are sill waiting for it to finish
        default="",
        scope=Scope.user_state_summary,
    )
    last_export_result = Dict(
        # The info dict returned by the most recent successful export.
        # If the export failed, it will have an "error" key set.
        default=None,
        scope=Scope.user_state_summary,
    )

    @XBlock.json_handler
    def csv_export(self, data, suffix=''):
        """
        Asynchronously export given data as a CSV file.
        """
        # Launch task
        from .tasks import export_csv_data  # Import here since this is edX LMS specific

        # Make sure we nail down our state before sending off an asynchronous task.
        async_result = export_csv_data.delay(
            unicode(getattr(self.scope_ids, 'usage_id', None)),
            unicode(getattr(self.runtime, 'course_id', 'course_id')),
        )
        if not async_result.ready():
            self.active_export_task_id = async_result.id
        else:
            self._store_export_result(async_result)

        return self._get_export_status()

    @XBlock.json_handler
    def get_export_status(self, data, suffix=''):
        """
        Return current export's pending status, previous result,
        and the download URL.
        """
        return self._get_export_status()

    def _get_export_status(self):
        self.check_pending_export()
        return {
            'export_pending': bool(self.active_export_task_id),
            'last_export_result': self.last_export_result,
            'download_url': self.download_url_for_last_report,
        }

    def check_pending_export(self):
        """
        If we're waiting for an export, see if it has finished, and if so, get the result.
        """
        from .tasks import export_csv_data  # Import here since this is edX LMS specific
        if self.active_export_task_id:
            async_result = export_csv_data.AsyncResult(self.active_export_task_id)
            if async_result.ready():
                self._store_export_result(async_result)

    @property
    def download_url_for_last_report(self):
        """ Get the URL for the last report, if any """
        from lms.djangoapps.instructor_task.models import ReportStore  # pylint: disable=import-error

        # Unfortunately this is a bit inefficient due to the ReportStore API
        if not self.last_export_result or self.last_export_result['error'] is not None:
            return None

        report_store = ReportStore.from_config(config_name='GRADES_DOWNLOAD')
        course_key = getattr(self.scope_ids.usage_id, 'course_key', None)
        return dict(report_store.links_for(course_key)).get(self.last_export_result['report_filename'])

    def student_module_queryset(self):
        from courseware.models import StudentModule  # pylint: disable=import-error
        return StudentModule.objects.select_related('student').filter(
            course_id=self.runtime.course_id,
            module_state_key=self.scope_ids.usage_id,
        ).order_by('-modified')

    def _store_export_result(self, task_result):
        """ Given an AsyncResult or EagerResult, save it. """
        self.active_export_task_id = ''
        if task_result.successful():
            if isinstance(task_result.result, dict) and not task_result.result.get('error'):
                self.last_export_result = task_result.result
            else:
                self.last_export_result = {'error': u'Unexpected result: {}'.format(repr(task_result.result))}
        else:
            self.last_export_result = {'error': unicode(task_result.result)}

    def prepare_data(self):
        """
        Return a two-dimensional list containing cells of data ready for CSV export.
        """
        raise NotImplementedError

    def get_filename(self):
        """
        Return a string to be used as the filename for the CSV export.
        """
        return u"{}-data-export-{}.csv".format(self.display_name.lower(), time.strftime("%Y-%m-%d-%H%M%S", time.gmtime(time.time())))


class TallyMixin(object):
    """
    Manages allying.
    """

    __metaclass__ = ABCMeta

    @abstractmethod
    def clean_tally(self):
        pass

    @abstractmethod
    def decrement_tally(self, choices):
        pass

    @abstractmethod
    def increment_tally(self, choices):
        pass

    @abstractmethod
    def tally_detail(self):
        pass


class ListOrString(List):
    """
    Patch xblock.fields.List to accept either a list or scalar.
    """
    def from_json(self, value):
        if value is None or isinstance(value, list):
            return value
        elif isinstance(value, basestring) or isinstance(value, str):
            return [value]
        else:
            raise TypeError('Value stored in a List must be None or a list, found %s' % type(value))


class PollBase(XBlock, ResourceMixin, PublishEventMixin, TallyMixin):
    """
    Base class for Poll-like XBlocks.
    """
    has_author_view = True

    event_namespace = 'xblock.pollbase'
    private_results = Boolean(default=False, help=_("Whether or not to display results to the user."))
    multiple_choices = Boolean(default=False, help=_("Whether or not to allow multiple selections."))
    max_submissions = Integer(default=1, help=_("The maximum number of times a user may send a submission."))
    submissions_count = Integer(
        default=0, help=_("Number of times the user has sent a submission."), scope=Scope.user_state
    )
    feedback = String(default='', help=_("Text to display after the user votes."))

    def send_vote_event(self, choice_data):
        # Let the LMS know the user has answered the poll.
        self.runtime.publish(self, 'progress', {})
        # The SDK doesn't set url_name.
        event_dict = {'url_name': getattr(self, 'url_name', '')}
        event_dict.update(choice_data)
        self.publish_event_from_dict(
            self.event_namespace + '.submitted',
            event_dict,
        )

    @staticmethod
    def any_image(field):
        """
        Find out if any answer has an image, since it affects layout.
        """
        return any(value['img'] for key, value in field)

    @staticmethod
    def markdown_items(items):
        """
        Convert all items' labels into markdown.
        """
        return [(key, {
            'label': markdown(value['label']),
            'img': value['img'],
            'img_alt': value.get('img_alt')
        }) for key, value in items]

    def _get_block_id(self):
        """
        Return unique ID of this block. Useful for HTML ID attributes.

        Works both in LMS/Studio and workbench runtimes:
        - In LMS/Studio, use the location.html_id method.
        - In the workbench, use the usage_id.
        """
        if hasattr(self, 'location'):
            return self.location.html_id()  # pylint: disable=no-member

        return unicode(self.scope_ids.usage_id)

    def img_alt_mandatory(self):
        """
        Determine whether alt attributes for images are configured to be mandatory.  Defaults to True.
        """
        settings_service = self.runtime.service(self, "settings")
        if not settings_service:
            return True
        xblock_settings = settings_service.get_settings_bucket(self)
        return xblock_settings.get('IMG_ALT_MANDATORY', True)

    def gather_items(self, data, result, noun, field, image=True):
        """
        Gathers a set of label-img pairs from a data dict and puts them in order.
        """
        items = []
        if field not in data or not isinstance(data[field], list):
            source_items = []
            result['success'] = False
            error_message = self.ugettext(
                # Translators: {field} is either "answers" or "questions".
                "'{field}' is not present, or not a JSON array."
            ).format(field=field)
            result['errors'].append(error_message)
        else:
            source_items = data[field]

        # Make sure all components are present and clean them.
        for item in source_items:
            if not isinstance(item, dict):
                result['success'] = False
                error_message = self.ugettext(
                    # Translators: {noun} is either "Answer" or "Question". {item} identifies the answer or question.
                    "{noun} {item} not a javascript object!"
                ).format(noun=noun, item=item)
                result['errors'].append(error_message)
                continue
            key = item.get('key', '').strip()
            if not key:
                result['success'] = False
                error_message = self.ugettext(
                    # Translators: {noun} is either "Answer" or "Question". {item} identifies the answer or question.
                    "{noun} {item} contains no key."
                ).format(noun=noun, item=item)
                result['errors'].append(error_message)
            image_link = item.get('img', '').strip()
            image_alt = item.get('img_alt', '').strip()
            label = item.get('label', '').strip()
            if not label:
                if image and not image_link:
                    result['success'] = False
                    error_message = self.ugettext(
                        # Translators: {noun} is either "Answer" or "Question".
                        # {noun_lower} is the lowercase version of {noun}.
                        "{noun} has no text or img. Please make sure all {noun_lower}s have one or the other, or both."
                    ).format(noun=noun, noun_lower=noun.lower())
                    result['errors'].append(error_message)
                elif not image:
                    result['success'] = False
                    # If there's a bug in the code or the user just forgot to relabel a question,
                    # votes could be accidentally lost if we assume the omission was an
                    # intended deletion.
                    error_message = self.ugettext(
                        # Translators: {noun} is either "Answer" or "Question".
                        # {noun_lower} is the lowercase version of {noun}.
                        "{noun} was added with no label. All {noun_lower}s must have labels. Please check the form. "
                        "Check the form and explicitly delete {noun_lower}s if not needed."
                    ).format(noun=noun, noun_lower=noun.lower())
                    result['errors'].append(error_message)
            if image_link and not image_alt and self.img_alt_mandatory():
                result['success'] = False
                result['errors'].append(
                    self.ugettext(
                        "All images must have an alternative text describing the image in a way "
                        "that would allow someone to answer the poll if the image did not load."
                    )
                )
            if image:
                items.append((key, {'label': label, 'img': image_link, 'img_alt': image_alt}))
            else:
                items.append([key, label])

        if not items:
            error_message = self.ugettext(
                # Translators: "{noun_lower} is either "answer" or "question".
                "You must include at least one {noun_lower}."
            ).format(noun_lower=noun.lower())
            result['errors'].append(error_message)
            result['success'] = False

        return items

    def can_vote(self):
        """
        Checks to see if the user is permitted to vote. This may not be the case if they used up their max_submissions.
        """
        return self.max_submissions == 0 or self.submissions_count < self.max_submissions

    def can_view_private_results(self):
        """
        Checks to see if the user has permissions to view private results.
        This only works inside the LMS.
        """
        if not hasattr(self.runtime, 'user_is_staff'):
            return False

        # Course staff users have permission to view results.
        if self.runtime.user_is_staff:
            return True

        # Check if user is member of a group that is explicitly granted
        # permission to view the results through django configuration.
        if not HAS_GROUP_PROFILE:
            return False
        group_names = getattr(settings, 'XBLOCK_POLL_EXTRA_VIEW_GROUPS', [])
        if not group_names:
            return False
        user = self.runtime.get_real_user(self.runtime.anonymous_student_id)
        group_ids = user.groups.values_list('id', flat=True)
        return GroupProfile.objects.filter(group_id__in=group_ids, name__in=group_names).exists()

    @staticmethod
    def get_max_submissions(ugettext, data, result, private_results):
        """
        Gets the value of 'max_submissions' from studio submitted AJAX data, and checks for conflicts
        with private_results, which may not be False when max_submissions is not 1, since that would mean
        the student could change their answer based on other students' answers.
        """
        try:
            max_submissions = int(data['max_submissions'])
        except (ValueError, KeyError):
            max_submissions = 1
            result['success'] = False
            result['errors'].append(ugettext('Maximum Submissions missing or not an integer.'))

        # Better to send an error than to confuse the user by thinking this would work.
        if (max_submissions != 1) and not private_results:
            result['success'] = False
            result['errors'].append(ugettext("Private results may not be False when Maximum Submissions is not 1."))
        return max_submissions

    @classmethod
    def static_replace_json_handler(cls, func):
        """A JSON handler that replace all static pseudo-URLs by the actual paths.

        The object returned by func is JSON-serialised, and the resulting string is passed to
        replace_static_urls() to perform regex-based URL replacing.

        We would prefer to explicitly call an API function on single image URLs, but such a function
        is not exposed by the LMS API, so we have to fall back to this slightly hacky implementation.
        """

        @cls.json_handler
        @functools.wraps(func)
        def wrapper(self, request_json, suffix=''):
            response = json.dumps(func(self, request_json, suffix))
            response = replace_static_urls(response, course_id=self.runtime.course_id)
            return Response(response, content_type='application/json')

        if HAS_STATIC_REPLACE:
            # Only use URL translation if it is available
            return wrapper
        # Otherwise fall back to a standard JSON handler
        return cls.json_handler(func)


@XBlock.wants('settings')
@XBlock.needs('i18n')
class PollBlock(PollBase, CSVExportMixin):
    """
    Poll XBlock. Allows a teacher to poll users, and presents the results so
    far of the poll to the user when finished.
    """
    # pylint: disable=too-many-instance-attributes

    display_name = String(default=_('Poll'))
    question = String(default=_('What is your favorite color?'))
    # This will be converted into an OrderedDict.
    # Key, (Label, Image path)
    answers = List(
        default=[
            ('R', {'label': _('Red'), 'img': None, 'img_alt': None}),
            ('B', {'label': _('Blue'), 'img': None, 'img_alt': None}),
            ('G', {'label': _('Green'), 'img': None, 'img_alt': None}),
            ('O', {'label': _('Other'), 'img': None, 'img_alt': None}),
        ],
        scope=Scope.settings, help=_("The answer options on this poll.")
    )
    # For compatibility we support storing either a single choice (string) or
    # multiple choices (list) depending on the `multiple_choices` flag.
    # Default to an empty list (no choice made yet).
    choice = ListOrString(default=[], scope=Scope.user_state, help=_("The student's answer(s)"))
    event_namespace = 'xblock.poll'

    tally = Dict(default={'R': 0, 'B': 0, 'G': 0, 'O': 0},
                 scope=Scope.user_state_summary,
                 help=_("Total tally of answers from students."))
    tally_count = Integer(default=0, scope=Scope.user_state_summary, help=_("Total number of votes."))

    def patch_tally_count(self):
        """
        Recalculate tally_count from the tally dictionary.
        """
        if self.tally_count:
            return
        self.tally_count = sum(int(v) for v in self.tally.values())

    def clean_tally(self):
        """
        Cleans the tally. Scoping prevents us from modifying this in the studio
        and in the LMS the way we want to without undesirable side effects. So
        we just clean it up on first access within the LMS, in case the studio
        has made changes to the answers.
        """
        answers = dict(self.answers)
        for key in answers:
            if key not in self.tally:
                self.tally[key] = 0

        for key in self.tally.keys():
            if key not in answers:
                del self.tally[key]

    def decrement_tally(self, choices):
        if not choices:
            return

        if not isinstance(choices, (list, tuple)):
            choices = [choices]

        for choice in choices:
            if choice in self.tally and self.tally[choice] > 0:
                self.tally[choice] -= 1

        if self.tally_count > 0:
            self.tally_count -= 1

    def increment_tally(self, choices):
        if not choices:
            return

        if not isinstance(choices, (list, tuple)):
            choices = [choices]

        for choice in choices:
            if choice in self.tally:
                self.tally[choice] += 1
            else:
                self.tally[choice] = 1

        self.tally_count += 1

    def tally_detail(self):
        """
        Return a detailed dictionary from the stored tally that the
        Handlebars template can use.
        """
        tally = []
        answers = OrderedDict(self.markdown_items(self.answers))
        total = 0
        self.clean_tally()
        source_tally = self.tally
        for key, value in answers.items():
            count = int(source_tally[key])
            tally.append({
                'count': count,
                'answer': value['label'],
                'img': value['img'],
                'img_alt': value.get('img_alt'),
                'key': key,
                'first': False,
                'choice': False,
                'last': False,
            })
            total += count

        total = self.tally_count or total

        choice = self.get_choice()
        for answer in tally:
            if answer['key'] in choice:
                answer['choice'] = True
            try:
                answer['percent'] = round(answer['count'] / float(total) * 100)
            except ZeroDivisionError:
                answer['percent'] = 0

        tally.sort(key=lambda x: x['count'], reverse=True)
        # This should always be true, but on the off chance there are
        # no answers...
        if tally:
            # Mark the first and last items to make things easier for Handlebars.
            tally[0]['first'] = True
            tally[-1]['last'] = True
        return tally, total

    def get_choice(self):
        """
        It's possible for the choice to have been removed since
        the student answered the poll. We don't want to take away
        the user's progress, but they should be able to vote again.
        """
        answers = dict(self.answers)

        # If stored as a list/tuple, filter invalid keys
        if isinstance(self.choice, (list, tuple)):
            valid = [c for c in self.choice if c in answers]
            return valid or []

        # Stored as a scalar (legacy support)
        if self.choice in answers:
            return [self.choice]

        return []

    def author_view(self, context=None):
        """
        Used to hide CSV export in Studio view
        """
        context['studio_edit'] = True
        return self.student_view(context)

    @XBlock.supports("multi_device")  # Mark as mobile-friendly
    def student_view(self, context=None):
        """
        The primary view of the PollBlock, shown to students
        when viewing courses.
        """
        if not context:
            context = {}
        js_template = self.resource_string(
            '/public/handlebars/poll_results.handlebars')

        choice = self.get_choice()

        context.update({
            'choice': choice,
            'answers': self.markdown_items(self.answers),
            'question': markdown(self.question),
            'private_results': self.private_results,
            'multiple_choices': self.multiple_choices,
            # Mustache is treating an empty string as true.
            'feedback': markdown(self.feedback) or False,
            'js_template': js_template,
            'any_img': self.any_image(self.answers),
            'display_name': self.display_name,
            'can_vote': self.can_vote(),
            'max_submissions': self.max_submissions,
            'submissions_count': self.submissions_count,
            'can_view_private_results': self.can_view_private_results(),
            # a11y: Transfer block ID to enable creating unique ids for questions and answers in the template
            'block_id': self._get_block_id(),
        })

        if choice:
            detail, total = self.tally_detail()
            context.update({'tally': detail, 'total': total, 'plural': total > 1})

        return self.create_fragment(
            context, "public/html/poll.html", "public/css/poll.css",
            "public/js/poll.js", "PollBlock")

    def student_view_data(self, context=None):
        """
        Returns a JSON representation of the poll Xblock, that can be retrieved
        using Course Block API.
        """
        return {
            'question': self.question,
            'answers': self.answers,
            'max_submissions': self.max_submissions,
            'private_results': self.private_results,
            'multiple_choices': self.multiple_choices,
            'feedback': self.feedback,
        }

    @XBlock.handler
    def student_view_user_state(self, data, suffix=''):
        """
        Returns a JSON representation of the student data for Poll Xblock
        """
        response = {
            'choice': self.get_choice(),
            'tally': self.tally,
            'tally_count': self.tally_count,
            'submissions_count': self.submissions_count,
        }

        return Response(
            json.dumps(response),
            content_type='application/json',
            charset='utf8'
        )

    def studio_view(self, context=None):
        if not context:
            context = {}

        js_template = self.resource_string('/public/handlebars/poll_studio.handlebars')
        context.update({
            'question': self.question,
            'display_name': self.display_name,
            'private_results': self.private_results,
            'multiple_choices': self.multiple_choices,
            'feedback': self.feedback,
            'js_template': js_template,
            'max_submissions': self.max_submissions,
        })
        return self.create_fragment(
            context, "public/html/poll_edit.html",
            "/public/css/poll_edit.css", "public/js/poll_edit.js", "PollEdit")

    @XBlock.json_handler
    def load_answers(self, data, suffix=''):
        return {
            'items': [
                {
                    'key': key, 'text': value['label'], 'img': value['img'], 'img_alt': value.get('img_alt'),
                    'noun': 'answer', 'image': True,
                }
                for key, value in self.answers
            ],
        }

    @PollBase.static_replace_json_handler
    def get_results(self, data, suffix=''):
        if self.private_results and not self.can_view_private_results():
            detail, total = {}, None
        else:
            self.publish_event_from_dict(self.event_namespace + '.view_results', {})
            detail, total = self.tally_detail()
        return {
            'question': markdown(self.question),
            'tally': detail,
            'total': total,
            'feedback': markdown(self.feedback),
            'plural': total > 1,
            'display_name': self.display_name,
            'any_img': self.any_image(self.answers),
            # a11y: Transfer block ID to enable creating unique ids for questions and answers in the template
            'block_id': self._get_block_id(),
        }

    @XBlock.json_handler
    def vote(self, data, suffix=''):
        """
        Sets the user's vote.
        """
        result = {'success': False, 'errors': []}
        old_choice = self.get_choice()
        if old_choice and not self.private_results:
            result['errors'].append(self.ugettext('You have already voted in this poll.'))
            return result
        try:
            choice = data['choice']
        except KeyError:
            result['errors'].append(self.ugettext('Answer not included with request.'))
            return result
        answers_dict = OrderedDict(self.answers)

        incoming = list(choice) if isinstance(choice, (list, tuple)) else [choice]

        try:
            for c in incoming:
                if c not in answers_dict:
                    raise KeyError(c)
        except KeyError as err:
            result['errors'].append(
                self.ugettext(
                    # Translators: {choice} uniquely identifies a specific answer belonging to a poll or survey.
                    'No key "{choice}" in answers table.'
                ).format(choice=err)
            )
            return result

        self.patch_tally_count()

        if not old_choice:
            # Reset submissions count if old choice is bogus.
            self.submissions_count = 0

        if not self.can_vote():
            result['errors'].append(self.ugettext('You have already voted as many times as you are allowed.'))
            return result

        self.clean_tally()
        self.decrement_tally(old_choice)
        self.increment_tally(incoming)

        self.choice = incoming
        self.submissions_count += 1

        result['success'] = True
        result['can_vote'] = self.can_vote()
        result['submissions_count'] = self.submissions_count
        result['max_submissions'] = self.max_submissions

        self.send_vote_event({'choices': self.get_choice()})

        return result

    @XBlock.json_handler
    def studio_submit(self, data, suffix=''):
        result = {'success': True, 'errors': []}
        question = data.get('question', '').strip()
        feedback = data.get('feedback', '').strip()
        private_results = bool(data.get('private_results', False))
        multiple_choices = bool(data.get('multiple_choices', False))

        max_submissions = self.get_max_submissions(self.ugettext, data, result, private_results)

        display_name = data.get('display_name', '').strip()
        if not question:
            result['errors'].append(self.ugettext("You must specify a question."))
            result['success'] = False

        answers = self.gather_items(data, result, self.ugettext('Answer'), 'answers')

        if not result['success']:
            return result

        self.answers = answers
        self.question = question
        self.feedback = feedback
        self.private_results = private_results
        self.multiple_choices = multiple_choices
        self.display_name = display_name
        self.max_submissions = max_submissions

        # Tally will not be updated until the next attempt to use it, per
        # scoping limitations.

        return result

    @XBlock.json_handler
    def student_voted(self, data, suffix=''):
        return {
            'voted': bool(self.get_choice()),
            'private_results': self.private_results
        }

    @staticmethod
    def workbench_scenarios():
        """
        Canned scenarios for display in the workbench.
        """
        return [
            ("Default Poll",
             """
             <poll />
             """),
            ("Customized Poll",
             """
             <poll tally="{'long': 20, 'short': 29, 'not_saying': 15, 'longer' : 35}"
                 question="## How long have you been studying with us?"
                 answers='[["longt", {"label": "A very long time", "img": null, "img_alt": null}],
                           ["short", {"label": "Not very long", "img": null, "img_alt": null}],
                           ["not_saying", {"label": "I shall not say", "img": null, "img_alt": null}],
                           ["longer", {"label": "Longer than you", "img": null, "img_alt": null}]]'
                 feedback="### Thank you&#10;&#10;for being a valued student."/>
             """),
        ]

    def prepare_data(self):
        header_row = ['user_id', 'username', 'user_email', 'Answer Date', 'question', 'answer']
        data = {}
        answers_dict = dict(self.answers)
        for sm in self.student_module_queryset():
            raw_state = json.loads(sm.state)
            choice = raw_state.get('choice')

            if isinstance(choice, (list, tuple)):
                answer_label = '; '.join([answers_dict.get(c, {}).get('label', str(c)) for c in choice])
            else:
                answer_label = answers_dict.get(choice, {}).get('label', str(choice))
            if sm.student.id not in data:
                data[sm.student.id] = [
                    sm.student.id,
                    sm.student.username,
                    sm.student.email,
                    sm.modified.strftime("%Y-%m-%d %H:%M:%S"),
                    self.question,
                    answer_label,
                ]
        return [header_row] + data.values()
