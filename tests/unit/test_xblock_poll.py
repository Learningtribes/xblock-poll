from __future__ import absolute_import

import unittest
import json
from datetime import datetime

from mock import Mock, patch
from xblock.field_data import DictFieldData

from poll.poll import ListOrString, PollBlock, SurveyBlock
from ..utils import MockRuntime, make_request


class TestPollBlock(unittest.TestCase):
    """
    Tests for XBlock Poll.
    """
    def setUp(self):
        """
        Test case setup
        """
        super(TestPollBlock, self).setUp()
        self.runtime = MockRuntime()
        self.poll_data = {
            'display_name': 'My Poll',
            'question': 'What is your favorite color?',
            'answers': [
                ['R', {'label': 'Red'}],
                ['B', {'label': 'Blue'}],
                ['G', {'label': 'Green'}],
                ['O', {'label': 'Other'}],
            ],
            'submissions_count': 5,
            'max_submissions': 1,
            'private_results': False,
            'feedback': 'My Feedback',
        }
        self.poll_block = PollBlock(
            self.runtime,
            DictFieldData(self.poll_data),
            None
        )

    def test_student_view_data(self):
        """
        Test the student_view_data results.
        """
        expected_poll_data = {
            'question': self.poll_data['question'],
            'answers': self.poll_data['answers'],
            'max_submissions': self.poll_data['max_submissions'],
            'private_results': self.poll_data['private_results'],
            'multiple_choices': False,
            'feedback': self.poll_data['feedback'],
        }

        student_view_data = self.poll_block.student_view_data()
        self.assertEqual(student_view_data, expected_poll_data)

    def test_student_view_user_state_handler(self):
        """
        Test the student_view_user_state handler results.
        """
        response = json.loads(
            self.poll_block.handle(
                'student_view_user_state',
                make_request('', method='GET')
            ).body.decode('utf-8')
        )
        expected_response = {
            u'choice': [],
            u'submissions_count': 5,
            u'tally': {'R': 0, 'B': 0, 'G': 0, 'O': 0},
            u'tally_count': 0,
        }
        self.assertEqual(response, expected_response)

    def test_list_or_string_accepts_text(self):
        """Legacy scalar choices remain readable on Python 2 and Python 3."""
        self.assertEqual(['R'], ListOrString().from_json('R'))

    def test_clean_tally_removes_unknown_answers(self):
        """Cleaning a tally may delete keys while iterating on Python 3."""
        self.poll_block.tally = {'R': 1, 'removed': 2}
        self.poll_block.clean_tally()
        self.assertEqual({'R': 1, 'B': 0, 'G': 0, 'O': 0}, self.poll_block.tally)

    def test_prepare_data_returns_list(self):
        """CSV export rows are a concrete list rather than a Python 3 view."""
        student = Mock(id=1, username='student', email='student@example.com')
        state = Mock(
            student=student,
            state='{"choice": ["R"]}',
            modified=datetime(2020, 1, 2, 3, 4, 5),
        )
        with patch.object(self.poll_block, 'student_module_queryset', return_value=[state]):
            rows = self.poll_block.prepare_data()

        self.assertIsInstance(rows, list)
        self.assertEqual(rows[1][-1], 'Red')


class TestSurveyBlock(unittest.TestCase):
    """
    Tests for XBlock Survey.
    """
    def setUp(self):
        """
        Test case setup
        """
        super(TestSurveyBlock, self).setUp()
        self.runtime = MockRuntime()
        self.survery_data = {
            'display_name': 'My Survey',
            'questions': [
                ['enjoy', {'label': 'Are you enjoying the course?'}],
                ['recommend', {'label': 'Would you recommend this course to your friends?'}],
                ['learn', {'label': 'Do you think you will learn a lot?'}]
            ],
            'answers': [
                ['Y', 'Yes'],
                ['N', 'No'],
                ['M', 'Maybe']
            ],
            'submissions_count': 5,
            'max_submissions': 1,
            'private_results': False,
            'feedback': 'My Feedback',
            'block_name': 'My Block Name',
        }
        self.survey_block = SurveyBlock(
            self.runtime,
            DictFieldData(self.survery_data),
            None
        )

    def test_student_view_data(self):
        """
        Test the student_view_data results.
        """
        expected_survery_data = {
            'questions': self.survery_data['questions'],
            'answers': self.survery_data['answers'],
            'max_submissions': self.survery_data['max_submissions'],
            'private_results': self.survery_data['private_results'],
            'feedback': self.survery_data['feedback'],
            'block_name': self.survery_data['block_name'],
        }

        student_view_data = self.survey_block.student_view_data()
        self.assertEqual(student_view_data, expected_survery_data)

    def test_student_view_user_state_handler(self):
        """
        Test the student_view_user_state handler results.
        """
        response = json.loads(
            self.survey_block.handle(
                'student_view_user_state',
                make_request('', method='GET')
            ).body
        )
        expected_response = {
            u'choices': None,
            u'submissions_count': 5,
            u'tally': {
                u'enjoy': {u'M': 0, u'N': 0, u'Y': 0},
                u'learn': {u'M': 0, u'N': 0, u'Y': 0},
                u'recommend': {u'M': 0, u'N': 0, u'Y': 0},
            },
        }
        self.assertEqual(response, expected_response)
