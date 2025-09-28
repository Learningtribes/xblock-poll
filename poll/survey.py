from collections import OrderedDict
import json
import time

from markdown import markdown
from webob import Response
from xblock.core import XBlock
from xblock.fields import Scope, String, Dict, List

from .poll import PollBase, CSVExportMixin
from .utils import _


class SurveyBlock(PollBase, CSVExportMixin):
    # pylint: disable=too-many-instance-attributes

    display_name = String(default=_('Survey'))
    # The display name affects how the block is labeled in the studio,
    # but either way we want it to say 'Poll' by default on the page.
    block_name = String(default=_('Poll'))
    answers = List(
        default=[
            ('Y', _('Yes')),
            ('N', _('No')),
            ('M', _('Maybe'))
        ],
        scope=Scope.settings, help=_("Answer choices for this Survey")
    )
    questions = List(
        default=[
            ('enjoy', {'label': _('Are you enjoying the course?'), 'img': None, 'img_alt': None}),
            ('recommend', {
                'label': _('Would you recommend this course to your friends?'),
                'img': None,
                'img_alt': None
            }),
            ('learn', {'label': _('Do you think you will learn a lot?'), 'img': None, 'img_alt': None}),
        ],
        scope=Scope.settings, help=_("Questions for this Survey")
    )
    tally = Dict(
        default={
            'enjoy': {'Y': 0, 'N': 0, 'M': 0}, 'recommend': {'Y': 0, 'N': 0, 'M': 0},
            'learn': {'Y': 0, 'N': 0, 'M': 0}},
        scope=Scope.user_state_summary,
        help=_("Total tally of answers from students.")
    )
    choices = Dict(help=_("The user's answers"), scope=Scope.user_state)
    event_namespace = 'xblock.survey'

    def author_view(self, context=None):
        """
        Used to hide CSV export in Studio view
        """
        context['studio_edit'] = True
        return self.student_view(context)

    @XBlock.supports("multi_device")  # Mark as mobile-friendly
    def student_view(self, context=None):
        """
        The primary view of the SurveyBlock, shown to students
        when viewing courses.
        """
        if not context:
            context = {}

        js_template = self.resource_string(
            '/public/handlebars/survey_results.handlebars')

        choices = self.get_choices()

        context.update({
            'choices': choices,
            # Offset so choices will always be True.
            'answers': self.answers,
            'answers_need_wrap': any((len(a[1]) > 20 for a in self.answers)),
            'js_template': js_template,
            'questions': self.renderable_answers(self.questions, choices),
            'private_results': self.private_results,
            'any_img': self.any_image(self.questions),
            # Mustache is treating an empty string as true.
            'feedback': markdown(self.feedback) or False,
            'block_name': self.block_name,
            'can_vote': self.can_vote(),
            'submissions_count': self.submissions_count,
            'max_submissions': self.max_submissions,
            'can_view_private_results': self.can_view_private_results(),
            # a11y: Transfer block ID to enable creating unique ids for questions and answers in the template
            'block_id': self._get_block_id(),
        })

        return self.create_fragment(
            context, "public/html/survey.html", "public/css/poll.css",
            "public/js/poll.js", "SurveyBlock")

    def student_view_data(self, context=None):
        """
        Returns a JSON representation of survey XBlock, that can be retrieved
        using Course Block API.
        """
        return {
            'questions': self.questions,
            'answers': self.answers,
            'max_submissions': self.max_submissions,
            'private_results': self.private_results,
            'block_name': self.block_name,
            'feedback': self.feedback,
        }

    @XBlock.handler
    def student_view_user_state(self, data, suffix=''):
        """
        Returns a JSON representation of the student data for Survey Xblock
        """
        response = {
            'choices': self.get_choices(),
            'tally': self.tally,
            'submissions_count': self.submissions_count,
        }

        return Response(
            json.dumps(response),
            content_type='application/json',
            charset='utf8'
        )

    def renderable_answers(self, questions, choices):
        """
        Render markdown for questions, and annotate with answers
        in the case of private_results.
        """
        choices = choices or {}
        markdown_questions = self.markdown_items(questions)
        for key, value in markdown_questions:
            value['choice'] = choices.get(key, None)
        return markdown_questions

    def studio_view(self, context=None):
        if not context:
            context = {}

        js_template = self.resource_string('/public/handlebars/poll_studio.handlebars')
        context.update({
            'feedback': self.feedback,
            'display_name': self.block_name,
            'private_results': self.private_results,
            'js_template': js_template,
            'max_submissions': self.max_submissions,
            'multiquestion': True,
        })
        return self.create_fragment(
            context, "public/html/poll_edit.html",
            "/public/css/poll_edit.css", "public/js/poll_edit.js", "SurveyEdit")

    def tally_detail(self):
        """
        Return a detailed dictionary from the stored tally that the
        Handlebars template can use.
        """
        tally = []
        questions = OrderedDict(self.markdown_items(self.questions))
        default_answers = OrderedDict([(answer, 0) for answer, __ in self.answers])
        choices = self.choices or {}
        total = 0
        self.clean_tally()
        source_tally = self.tally

        # The result should always be the same-- just grab the first one.
        for key, value in source_tally.items():
            total = sum(value.values())
            break

        for key, value in questions.items():
            # Order matters here.
            answer_set = OrderedDict(default_answers)
            answer_set.update(source_tally[key])
            tally.append({
                'label': value['label'],
                'img': value['img'],
                'img_alt': value.get('img_alt'),
                'answers': [
                    {
                        'count': count, 'choice': False,
                        'key': answer_key, 'top': False,
                    }
                    for answer_key, count in answer_set.items()],
                'key': key,
                'choice': False,
            })

        for question in tally:
            highest = 0
            top_index = None
            for index, answer in enumerate(question['answers']):
                if answer['key'] == choices.get(question['key']):
                    answer['choice'] = True
                # Find the most popular choice.
                if answer['count'] > highest:
                    top_index = index
                    highest = answer['count']
                try:
                    answer['percent'] = round(answer['count'] / float(total) * 100)
                except ZeroDivisionError:
                    answer['percent'] = 0
            if top_index is not None:
                question['answers'][top_index]['top'] = True

        return tally, total

    def clean_tally(self):
        """
        Cleans the tally. Scoping prevents us from modifying this in the studio
        and in the LMS the way we want to without undesirable side effects. So
        we just clean it up on first access within the LMS, in case the studio
        has made changes to the answers.
        """
        questions = dict(self.questions)
        answers = dict(self.answers)
        default_answers = {answer: 0 for answer in answers.keys()}
        for key in questions.keys():
            if key not in self.tally:
                self.tally[key] = dict(default_answers)
            else:
                # Answers may have changed, requiring an update for each
                # question.
                new_answers = dict(default_answers)
                new_answers.update(self.tally[key])
                for existing_key in self.tally[key]:
                    if existing_key not in default_answers:
                        del new_answers[existing_key]
                self.tally[key] = new_answers
        # Keys for questions that no longer exist can break calculations.
        for key in self.tally.keys():
            if key not in questions:
                del self.tally[key]

    def remove_vote(self):
        """
        If the poll has changed after a user has voted, remove their votes
        from the tally.

        This can only be done lazily-- once a user revisits, since we can't
        edit the tally in the studio due to scoping issues.

        This means a user's old votes may still count indefinitely after a
        change, should they never revisit.
        """
        questions = dict(self.questions)
        answers = dict(self.answers)
        for key, value in self.choices.items():
            if key in questions:
                if value in answers:
                    self.tally[key][value] -= 1
        self.choices = None
        self.save()

    def get_choices(self):
        """
        Gets the user's choices, if they're still valid.
        """
        questions = dict(self.questions)
        answers = dict(self.answers)
        if self.choices is None:
            return None
        if sorted(questions.keys()) != sorted(self.choices.keys()):
            self.remove_vote()
            return None
        for value in self.choices.values():
            if value not in answers:
                self.remove_vote()
                return None
        return self.choices

    @PollBase.static_replace_json_handler
    def get_results(self, data, suffix=''):
        if self.private_results and not self.can_view_private_results():
            detail, total = {}, None
        else:
            self.publish_event_from_dict(self.event_namespace + '.view_results', {})
            detail, total = self.tally_detail()
        return {
            'answers': [
                {'key': key, 'label': label} for key, label in self.answers
            ],
            'tally': detail,
            'total': total,
            'feedback': markdown(self.feedback),
            'plural': total > 1,
            'block_name': self.block_name,
            # a11y: Transfer block ID to enable creating unique ids for questions and answers in the template
            'block_id': self._get_block_id()
        }

    @XBlock.json_handler
    def load_answers(self, data, suffix=''):
        return {
            'items': [
                {
                    'key': key, 'text': value,
                    'noun': 'answer', 'image': False,
                }
                for key, value in self.answers
            ],
        }

    @XBlock.json_handler
    def load_questions(self, data, suffix=''):
        return {
            'items': [
                {
                    'key': key, 'text': value['label'], 'img': value['img'], 'img_alt': value.get('img_alt'),
                    'noun': 'question', 'image': True,
                }
                for key, value in self.questions
            ]
        }

    @XBlock.json_handler
    def vote(self, data, suffix=''):
        questions = dict(self.questions)
        answers = dict(self.answers)
        result = {'success': True, 'errors': []}
        choices = self.get_choices()
        if choices and not self.private_results:
            result['success'] = False
            result['errors'].append(self.ugettext("You have already voted in this poll."))

        if not choices:
            # Reset submissions count if choices are bogus.
            self.submissions_count = 0

        if not self.can_vote():
            result['success'] = False
            result['errors'].append(self.ugettext('You have already voted as many times as you are allowed.'))

        # Make sure the user has included all questions, and hasn't included
        # anything extra, which might indicate the questions have changed.
        if not sorted(data.keys()) == sorted(questions.keys()):
            result['success'] = False
            result['errors'].append(
                self.ugettext(
                    "Not all questions were included, or unknown questions were included. "
                    "Try refreshing and trying again."
                )
            )

        # Make sure the answer values are sane.
        for key, value in data.items():
            if value not in answers.keys():
                result['success'] = False
                result['errors'].append(
                    self.ugettext(
                        # Translators: {answer_key} uniquely identifies a specific answer belonging to a poll or survey.
                        # {question_key} uniquely identifies a specific question belonging to a poll or survey.
                        "Found unknown answer '{answer_key}' for question key '{question_key}'"
                    ).format(answer_key=key, question_key=value))

        if not result['success']:
            result['can_vote'] = self.can_vote()
            return result

        # Record the vote!
        if self.choices:
            self.remove_vote()
        self.choices = data
        self.clean_tally()
        for key, value in self.choices.items():
            self.tally[key][value] += 1
        self.submissions_count += 1

        self.send_vote_event({'choices': self.choices})
        result['can_vote'] = self.can_vote()
        result['submissions_count'] = self.submissions_count
        result['max_submissions'] = self.max_submissions

        return result

    @XBlock.json_handler
    def studio_submit(self, data, suffix=''):
        # I wonder if there's something for live validation feedback already.

        result = {'success': True, 'errors': []}
        feedback = data.get('feedback', '').strip()
        block_name = data.get('display_name', '').strip()
        private_results = bool(data.get('private_results', False))
        max_submissions = self.get_max_submissions(self.ugettext, data, result, private_results)

        answers = self.gather_items(data, result, self.ugettext('Answer'), 'answers', image=False)
        questions = self.gather_items(data, result, self.ugettext('Question'), 'questions')

        if not result['success']:
            return result

        self.answers = answers
        self.questions = questions
        self.feedback = feedback
        self.private_results = private_results
        self.max_submissions = max_submissions
        self.block_name = block_name

        # Tally will not be updated until the next attempt to use it, per
        # scoping limitations.

        return result

    @XBlock.json_handler
    def student_voted(self, data, suffix=''):
        return {
            'voted': self.get_choices() is not None,
            'private_results': self.private_results
        }

    @staticmethod
    def workbench_scenarios():
        """
        Canned scenarios for display in the workbench.
        """
        return [
            ("Default Survey",
             """
             <survey />
             """),
            ("Survey Functions",
             """
             <survey tally='{"q1": {"sa": 5, "a": 5, "n": 3, "d": 2, "sd": 5},
                             "q2": {"sa": 3, "a": 2, "n": 3, "d": 10, "sd": 2},
                             "q3": {"sa": 2, "a": 7, "n": 1, "d": 4, "sd": 6},
                             "q4": {"sa": 1, "a": 2, "n": 8, "d": 4, "sd": 5}}'
                 questions='[["q1", {"label": "I feel like this test will pass.", "img": null, "img_alt": null}],
                             ["q2", {"label": "I like testing software", "img": null, "img_alt": null}],
                             ["q3", {"label": "Testing is not necessary", "img": null, "img_alt": null}],
                             ["q4", {"label": "I would fake a test result to get software deployed.", "img": null,
                                     "img_alt": null}]]'
                 answers='[["sa", "Strongly Agree"], ["a", "Agree"], ["n", "Neutral"],
                           ["d", "Disagree"], ["sd", "Strongly Disagree"]]'
                 feedback="### Thank you&#10;&#10;for running the tests."/>
             """)
        ]

    def get_filename(self):
        return u"survey-data-export-{}.csv".format(time.strftime("%Y-%m-%d-%H%M%S", time.gmtime(time.time())))

    def prepare_data(self):
        header_row = ['user_id', 'username', 'user_email', 'Answer Date']
        sorted_questions = sorted(self.questions, key=lambda x: x[0])
        questions = [q[1]['label'] for q in sorted_questions]
        data = {}
        answers_dict = dict(self.answers)
        for sm in self.student_module_queryset():
            state = json.loads(sm.state)
            if sm.student.id not in data and state.get('choices'):
                row = [
                    sm.student.id,
                    sm.student.username,
                    sm.student.email,
                    sm.modified.strftime("%Y-%m-%d %H:%M:%S"),
                ]
                for q in sorted_questions:
                    choices = state.get('choices')
                    if choices:
                        choice = choices.get(q[0], None)
                        if choice is None:
                            row.append("")
                        else:
                            row.append(answers_dict[choice])
                data[sm.student.id] = row
        return [header_row + questions] + data.values()
