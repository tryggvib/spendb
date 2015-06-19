from spendb.tests.base import ControllerTestCase


class TestErrors(ControllerTestCase):

    def test_error_404(self):
        response = self.client.get('/akhkfhdjkhf/fgfdghfdh')
        assert 'SpenDB' in response.data
        assert 'Not Found' in response.data

    def test_error_403(self):
        response = self.client.post('/api/3/datasets')
        assert response.status_code == 403, response
