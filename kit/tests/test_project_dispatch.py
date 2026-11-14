"""Доставка заявки из базы в подходящую сессию Чифа и восстановление после сбоя."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kit import project_dispatch as dispatch
from kit.project_revisions import request_revision
from kit.project_vault import ProjectVault
from kit.task_metrics import TaskMetrics


class SessionService:
    """Хранит наблюдаемые вызовы службы, включая полученные до потери ответа."""

    def __init__(self, project):
        self.directory = project
        self.created = []
        self.prompts = []
        self.rows = []
        self.prompt_outcome = 'accepted'

    def capabilities(self):
        return {}

    def sessions(self):
        return list(self.rows)

    def call(self, method, parameters=None, data=None):
        if method == 'session.create':
            self.created.append(data)
            row = dict(data, id='ses_created', location={'directory': str(self.directory)})
            self.rows.append(row)
            return {'data': row}
        if method == 'session.get':
            return {'data': next(row for row in self.rows if row['id'] == parameters['sessionID'])}
        if method != 'session.prompt':
            raise AssertionError(f'Неожиданная операция службы: {method}')
        self.prompts.append((parameters['sessionID'], data))
        if self.prompt_outcome == 'timeout':
            raise TimeoutError('Служба приняла сообщение, соединение оборвалось до ответа.')
        if self.prompt_outcome == 'unknown':
            return {'data': {'id': 'msg_other'}}
        return {'data': {'id': data['id']}}


class ProjectDispatchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'Проект заказчика'
        self.root.mkdir()
        self.vault = ProjectVault.connect(self.root)
        self.vault.add('stage', 'release', 'Выпуск')
        self.vault.add('feature', 'review', 'Согласование', 'release')
        with self.vault.workspace('review').open('a', encoding='utf-8') as note:
            note.write('Показывать дату решения в карточке.')
        self.request = request_revision(self.vault, 'review')
        self.metrics = TaskMetrics(self.root)
        self.task = self.request['task_id']
        self.service = SessionService(self.root)
        self.patch_gateway = patch.object(dispatch, 'create_session_gateway', return_value=self.service)
        self.patch_gateway.start()
        self.addCleanup(self.patch_gateway.stop)
        self.model = {'providerID': 'local', 'id': 'chief'}
        model = patch.object(dispatch, '_chief_model', return_value=self.model)
        model.start()
        self.addCleanup(model.stop)

    def row(self, directory):
        return {'id': 'ses_recovered', 'title': f'Чиф · {self.task}', 'agent': 'build',
                'parentID': None, 'location': {'directory': str(directory)}}

    def test_send_creates_a_project_root_session_and_delivers_once_across_reopen(self):
        first = dispatch.dispatch_request(self.vault, self.request)
        repeated = dispatch.dispatch_request(ProjectVault.load(self.root), self.request)
        self.assertEqual(repeated, first)
        created, = self.service.created
        self.assertEqual(created['location'], {'directory': str(self.root)})
        self.assertIsNone(created['parentID'])
        self.assertEqual((created['agent'], created['model']), ('build', self.model))
        (session, message), = self.service.prompts
        self.assertEqual((session, message['id']), (first['session'], first['message']))
        for expected in (self.vault.project_id, 'release', 'review', self.request['text']):
            self.assertIn(expected, message['text'])
        start, = self.metrics.read(self.task)
        self.assertEqual((start['kind'], start['session']), ('start', session))

    def test_service_echoing_a_foreign_directory_cannot_admit_task(self):
        self.service.directory = self.root.parent
        with self.assertRaises(ValueError):
            dispatch.dispatch_request(self.vault, self.request)
        self.assertEqual(len(self.service.created), 1)
        self.assertEqual(self.service.prompts, [])
        self.assertFalse(self.metrics.task_dir(self.task).exists())

    def test_recovers_one_existing_project_session_without_creating_another(self):
        self.service.rows.append(self.row(self.root))
        result = dispatch.dispatch_request(self.vault, self.request)
        self.assertEqual(result['session'], 'ses_recovered')
        self.assertEqual(self.service.created, [])
        self.assertEqual(len(self.service.prompts), 1)
        self.assertEqual(self.metrics.read(self.task)[0]['session'], 'ses_recovered')

    def test_ambiguous_existing_sessions_require_resolution_before_delivery(self):
        self.service.rows.extend([self.row(self.root), dict(self.row(self.root), id='ses_another')])
        with self.assertRaises(ValueError):
            dispatch.dispatch_request(self.vault, self.request)
        self.assertEqual(self.service.created, [])
        self.assertEqual(self.service.prompts, [])

    def test_journal_recovery_checks_current_session_owner_before_delivery(self):
        row = self.row(self.root.parent)
        self.service.rows.append(row)
        self.metrics.start(self.task, session=row['id'], title=row['title'], track_delegations=True)
        with self.assertRaises(ValueError):
            dispatch.dispatch_request(self.vault, self.request)
        self.assertEqual(self.service.created, [])
        self.assertEqual(self.service.prompts, [])

    def test_request_from_another_project_cannot_use_an_identical_record_key(self):
        other_root = self.root.parent / 'Другой проект'
        other_root.mkdir()
        other = ProjectVault.connect(other_root)
        other.add('stage', 'release', 'Выпуск')
        other.add('feature', 'review', 'Согласование', 'release')
        with other.workspace('review').open('a', encoding='utf-8') as note:
            note.write('Изменение в другом проекте.')
        foreign = request_revision(other, 'review')
        with self.assertRaises(ValueError):
            dispatch.dispatch_request(self.vault, foreign)
        self.assertEqual(self.service.created, [])
        self.assertEqual(self.service.prompts, [])

    def assert_uncertain_request_is_not_resent(self, outcome, exception):
        self.service.prompt_outcome = outcome
        with self.assertRaises(exception):
            dispatch.dispatch_request(self.vault, self.request)
        self.service.prompt_outcome = 'accepted'
        with self.assertRaises(ValueError):
            dispatch.dispatch_request(ProjectVault.load(self.root), self.request)
        self.assertEqual(len(self.service.created), 1)
        self.assertEqual(len(self.service.prompts), 1)
        start, = self.metrics.read(self.task)
        self.assertEqual(start['kind'], 'start')
        self.assertFalse((self.metrics.task_dir(self.task) / 'documentation-delivery.json').exists())

    def test_timeout_after_possible_admission_does_not_resend(self):
        self.assert_uncertain_request_is_not_resent('timeout', TimeoutError)

    def test_mismatched_message_acknowledgement_does_not_resend(self):
        self.assert_uncertain_request_is_not_resent('unknown', ValueError)
