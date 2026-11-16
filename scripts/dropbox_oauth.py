"""Run locally once to authorize the Dropbox app and obtain a refresh token."""
from getpass import getpass
from dropbox import DropboxOAuth2FlowNoRedirect


def main():
    app_key = input('Dropbox app key: ').strip()
    app_secret = getpass('Dropbox app secret (hidden): ').strip()
    flow = DropboxOAuth2FlowNoRedirect(
        app_key, app_secret, token_access_type='offline',
        scope=['files.content.read', 'files.content.write', 'files.metadata.read'])
    print('\nOpen this URL in your browser and authorize the app:\n' + flow.start())
    code = input('\nPaste the authorization code: ').strip()
    result = flow.finish(code)
    if not result.refresh_token:
        raise RuntimeError('Dropbox did not return a refresh token.')
    print('\nCopy this into Streamlit Secrets; do not commit it to Git:\n')
    print('DROPBOX_REFRESH_TOKEN = "' + result.refresh_token + '"')


if __name__ == '__main__':
    main()
