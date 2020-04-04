#####################################
##### otter server start Script #####
#####################################

MISSING_PACKAGES = False

try:
    import os
    import json
    import yaml
    import hashlib
    import jwt
    import logging
    import contextlib
    import traceback
    import tornado.options
    import queries

    from io import StringIO
    from datetime import datetime
    from binascii import hexlify
    from tornado.httpserver import HTTPServer
    from tornado.web import Application, RequestHandler
    from tornado.auth import GoogleOAuth2Mixin
    from tornado.ioloop import IOLoop
    from tornado.queues import Queue
    from tornado.gen import sleep
    # from psycopg2 import connect, extensions

    from ..containers import grade_assignments
    from ..utils import connect_db

except ImportError:
    # don't need requirements to use otter without otter service
    MISSING_PACKAGES = True


def main(args):
    if MISSING_PACKAGES:
        raise ImportError(
            "Missing some packages required for otter service. "
            "Please install all requirements at "
            "https://raw.githubusercontent.com/ucbds-infra/otter-grader/master/requirements.txt"
        )

    user_queue = Queue()
    #NB_DIR = os.environ.get('NOTEBOOK_DIR')
    conn = None

    # assert args.config is not None, "no config provided"
    # with open(args.config) as f:
    #     config = yaml.load(f)

    class BaseHandler(tornado.web.RequestHandler):
        def get_current_user(self):
            return self.get_secure_cookie("user")

    class LoginHandler(BaseHandler):
        async def get(self):
            username = self.get_argument('username', True)
            password = self.get_argument('password', True)
            pw_hash = hashlib.sha256(password.encode()).hexdigest()
            account_check = await self.db.query(
                """
                SELECT * FROM users 
                WHERE username = %s AND password = %s
                """,
                [username, pw_hash]
            )
            if len(account_check) > 0:
                account_check.free()
                api_key = hexlify(os.urandom(32)).decode("utf-8")
                self.write(api_key)
                results = await self.db.query(
                    """
                    INSERT INTO users (api_keys, username, password) VALUES (%s, %s, %s)
                    ON CONFLICT (username)
                    DO UPDATE SET api_keys = array_append(users.api_keys, %s)
                    """,
                    [[api_key], username, pw_hash, api_key]
                )
                results.free()
            else:
                account_check.free()
                self.clear()
                self.set_status(401)
                self.finish()

        @property
        def db(self):
            return self.application.db

    class GoogleOAuth2LoginHandler(RequestHandler, GoogleOAuth2Mixin):
        async def get(self):
            self.settings["google_oauth"] = {
                "key": args.google_key, "secret": args.google_secret
            }
            if not self.get_argument('code', False):
                print("not found")
                return self.authorize_redirect(
                    redirect_uri=self.settings['auth_redirect_uri'],
                    client_id = args.google_key if args.google_key else self.settings['google_oauth']['key'],
                    client_secret = args.google_secret if args.google_secret else self.settings['google_oauth']['secret'],
                    scope=['email', 'profile'],
                    response_type='code',
                    extra_params={'approval_prompt': 'auto'}
                )
            else:
                print("found")
                resp = await self.get_authenticated_user(
                    redirect_uri=self.settings['auth_redirect_uri'],
                    code=self.get_argument('code')
                )
                api_key = resp['access_token']
                email = jwt.decode(resp['id_token'], verify=False)['email']
                results = await self.db.query(
                    """
                    INSERT INTO users (api_keys, email) VALUES (%s, %s)
                    ON CONFLICT (email) 
                    DO UPDATE SET api_keys = array_append(users.api_keys, %s)
                    """,
                    [[api_key], email, api_key]
                )
                results.free()

                self.render("templates/api_key.html", key=api_key)

        @property
        def db(self):
            return self.application.db

    class SubmissionHandler(RequestHandler):
        async def post(self):
            try:
                request = tornado.escape.json_decode(self.request.body)
                assert 'nb' in request.keys(), 'submission contains no notebook'
                assert 'api_key' in request.keys(), 'missing api key'

                notebook = request['nb']
                api_key = request['api_key']
                
                await self.submit(notebook, api_key)
            except Exception as e:
                print(e)
            self.finish()



        async def validate(self, notebook, api_key):
            # authenticate user with api_key
            results = await self.db.query("SELECT user_id FROM users WHERE %s=ANY(api_keys) LIMIT 1", [api_key])
            user_id = results.as_dict()['user_id'] if len(results) > 0 else None
            results.free()
            assert user_id, 'invalid api key'

            # rate limit one submission every 2 minutes
            results = await self.db.query("SELECT timestamp FROM submissions WHERE user_id=%s ORDER BY timestamp DESC LIMIT 1", [user_id])
            last_submitted = results.as_dict()['timestamp'] if len(results) > 0 else None
            results.free()

            if last_submitted:
                delta = datetime.utcnow() - last_submitted
                rate_limit = 120
                if delta.seconds < rate_limit:
                    self.write_error(429, message='Please wait {} second(s) before re-submitting.'.format(rate_limit - delta.seconds))
                    return


            # check valid Jupyter notebook
            assert all(key in notebook for key in ['metadata', 'nbformat', 'nbformat_minor', 'cells']), 'invalid Jupyter notebook'
            assert 'assignment_id' in notebook['metadata'], 'missing required metadata attribute: assignment_id'
            assignment_id = notebook['metadata']['assignment_id']
            
            results = await self.db.query("SELECT * FROM assignments WHERE assignment_id=%s LIMIT 1", [assignment_id])
            assert results, 'assignment_id {} not found on server'.format(assignment_id)
            assignment = results.as_dict()
            results.free()

            return (user_id, assignment['class_id'], assignment_id, assignment['assignment_name'])


        async def submit(self, notebook, api_key):
            try:
                user_id, class_id, assignment_id, assignment_name = await self.validate(notebook, api_key)
            except TypeError:
                print('failed validation')
                return
            except AssertionError as e:
                print(e)
                self.write_error(400, message=e)
                return

            print("Successfully received notebook data")

            # fetch next submission id
            results = await self.db.query("SELECT nextval(pg_get_serial_sequence('submissions', 'submission_id')) as id")
            submission_id = results.as_dict()['id']
            results.free()
            print("Successfully queries submission id")

            # save notebook to disk
            dir_path = os.path.join(
                self.settings['notebook_dir'],
                'submissions',
                'class-{}'.format(class_id),
                'assignment-{}'.format(assignment_id),
                'submission-{}'.format(submission_id)
            )
            file_path = os.path.join(dir_path, '{}.ipynb'.format(assignment_name))
            if not os.path.exists(dir_path):
                os.makedirs(dir_path)
            with open(file_path, 'w') as f:
                json.dump(notebook, f)

            print("Successfully saved nb to disk")
            
            # store submission to database
            results = await self.db.query("INSERT INTO submissions (submission_id, assignment_id, user_id, file_path, timestamp) VALUES (%s, %s, %s, %s, %s)",
                                                [submission_id, assignment_id, user_id, file_path, datetime.utcnow()])
            assert results, 'submission failed'
            results.free()

            print("Successfully stored to db")

            # queue user for grading
            await user_queue.put(user_id)
            print('queued user {}'.format(user_id))

            self.write('Submission {} received.'.format(submission_id))

        @property
        def db(self):
            return self.application.db

        def write_error(self, status_code, **kwargs):
            if 'message' in kwargs:
                self.write('Submission failed: {}'.format(kwargs['message']))
            else:
                self.write('Submission failed.')

    # def connect_db(host="localhost", username="otterservice", password="mypass"):
    #     global conn
    #     conn = connect(dbname='otter_db',
    #             user=username,
    #             host=host,
    #             password=password)
    #     conn.set_isolation_level(extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    #     return conn

    async def grade():
        cursor = conn.cursor()
        
        # # This can be moved to global
        # with open("conf.yml") as f:
        #     config = yaml.safe_load(f)
        
        async for user in user_queue:
            # Get current user's latest submission
            cursor.execute(
                """
                SELECT user_id, submission_id, assignment_id, file_path 
                FROM submissions 
                WHERE user_id = '{}' 
                ORDER BY timestamp 
                LIMIT 1
                """.format(user)
            )
            user_record = cursor.fetchall()      
            for row in user_record:
                user_id = int(row[0])
                submission_id = float(row[1])
                assignment_id = str(row[2])
                file_path = str(row[3])

            # # Get tests directory for assignment
            # tests_path = None
            # for assignment in config["assignments"]:
            #     if assignment[assignment_id] == submission_id:
            #         tests_path = assignment["tests_path"]

            # Run grading function in a docker container
            # TODO: fix arguments below, redirect stdout/stderr
            stdout = StringIO()
            with contextlib.redirect_stdout(stdout):
                df = grade_assignments(
                    tests_dir=None, 
                    notebooks_dir=file_path, 
                    id=assignment_id, 
                    image=assignment_id,
                    debug=True
                )
            stdout = stdout.getvalue()
            df_json_str = df.to_json()

            # Insert score into submissions table
            cursor.execute("INSERT INTO submissions \
                (submission_id, assignment_id, user_id, file_path, timestamp, score) \
                VALUES (%s, %s, %s, %s, %s, %s) \
                ON CONFLICT (submission_id) \
                DO UPDATE SET timestamp = %s, score = %s",
                [submission_id, assignment_id, user_id, file_path, datetime.utcnow(), df_json_str,
                datetime.utcnow(), df_json_str])

            with open(os.path.join(os.path.split(file_path)[0], "GRADING_STDOUT"), "w+") as f:
                f.write(stdout)
            
            # Set task done in queue
            user_queue.task_done()
        cursor.close()


    class Application(tornado.web.Application):
        def __init__(self, google_auth=True):
            # TODO: these shouldn't be separate can just assume both are configured
            # if google_auth:
            # TODO: Add config file
            # with open("conf.yml") as f:
            #     config = yaml.safe_load(f)
            config = {
                "google_auth_key" : "hello",
                "google_auth_secret" : "world",
                "notebook_dir" : "./submissions",
                "auth_redirect_uri" : "http://localhost:5000/auth/callback",
            }
            settings = dict(
                google_oauth={
                    'key': config['google_auth_key'],
                    'secret': config['google_auth_secret'],
                },
                notebook_dir = config['notebook_dir'],
                auth_redirect_uri = config['auth_redirect_uri']
            )
            handlers = [
                (r"/submit", SubmissionHandler),
                (r"/auth/google", GoogleOAuth2LoginHandler),
                (r"/auth/callback", GoogleOAuth2LoginHandler),
                (r"/auth", LoginHandler)
            ]
            tornado.web.Application.__init__(self, handlers, **settings)
            # else:
            #     # TODO: add personal auth
            #     handlers = [
            #         (r"/submit", SubmissionHandler),
            #         (r"/personal_auth", LoginHandler)
            #     ]
            #     tornado.web.Application.__init__(self, handlers)
            
            # Initialize database session
            self.db = queries.TornadoSession(queries.uri(
                host='localhost',
                port=5432,
                dbname='otter_db',
                user='root',
                password='root'
            ))

    # TODO: add arguments below
    conn = connect_db("localhost", "root", "root")
    port = 5000
    tornado.options.parse_command_line()
    server = HTTPServer(Application(google_auth=True))
    server.listen(port)
    print("Listening on port {}".format(port))
    IOLoop.current().spawn_callback(grade)
    IOLoop.current().start()