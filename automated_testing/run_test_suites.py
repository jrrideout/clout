#!/usr/bin/env python
from __future__ import division

__author__ = "Jai Ram Rideout"
__copyright__ = "Copyright 2012, The QIIME project"
__credits__ = ["Jai Ram Rideout"]
__license__ = "GPL"
__version__ = "1.5.0-dev"
__maintainer__ = "Jai Ram Rideout"
__email__ = "jai.rideout@gmail.com"
__status__ = "Development"

"""Contains functions used in the run_test_suites.py script."""

from signal import SIGTERM
from email.Encoders import encode_base64
from email.MIMEBase import MIMEBase
from email.MIMEMultipart import MIMEMultipart
from email.mime.text import MIMEText
from email.Utils import formatdate
from os import killpg, setsid
from smtplib import SMTP
from subprocess import PIPE, Popen
from tempfile import TemporaryFile
from threading import Lock, Thread

def run_test_suites(config_f, sc_config_fp, recipients_f, email_settings_f,
                    user, cluster_tag, cluster_template=None,
                    setup_timeout=20.0, test_suites_timeout=240.0,
                    teardown_timeout=20.0, sc_exe_fp='starcluster'):
    """Runs the suite(s) of tests and emails the results to the recipients.

    This function does not return anything. This function is not unit-tested
    because there isn't a clean way to test it since it sends an email, starts
    up a cluster on Amazon EC2, etc. Nearly every other 'private' function that
    this function calls has been extensively unit-tested (whenever possible).
    Thus, the amount of untested code has been minimized and contained here.

    Arguments:
        config_f - the input configuration file describing the test suites to
            be run
        sc_config_fp - the starcluster config filepath that will be used to
            start/terminate the remote cluster that the tests will be run on
        recipients_f - the file containing email addresses of those who should
            receive the test suite results
        email_settings_f - the file containing email (SMTP) settings to allow
            the script to send an email
        user - the user who the tests should be run as on the remote cluster (a
            string)
        cluster_tag - the starcluster cluster tag to use when creating the
            remote cluster (a string)
        cluster_template - the starcluster cluster template to use in the
            starcluster config file. If not provided, the default cluster
            template in the starcluster config file will be used
        setup_timeout - the number of minutes to allow the cluster to be set up
            before aborting and attempting to terminate it. Must be a float, to
            allow for fractions of a minute
        test_suites_timeout - the number of minutes to allow *all* test suites
            to run before terminating the cluster. Must be a float, to allow
            for fractions of a minute
        teardown_timeout - the number of minutes to allow the cluster to be
            terminated before aborting. Must be a float, to allow for fractions
            of a minute
        sc_exe_fp - path to the starcluster executable
    """
    if setup_timeout <= 0 or test_suites_timeout <= 0 or teardown_timeout <= 0:
        raise ValueError("The timeout (in minutes) must be greater than zero.")

    # Parse the various configuration files first so that we know if there's
    # any outstanding problems with file formats before continuing.
    test_suites = _parse_config_file(config_f)
    recipients = _parse_email_list(recipients_f)
    email_settings = _parse_email_settings(email_settings_f)

    # Get the commands that need to be executed (these include launching a
    # cluster, running the test suites, and terminating the cluster).
    setup_cmds, test_suites_cmds, teardown_cmds = \
            _build_test_execution_commands(test_suites, sc_config_fp, user,
                                           cluster_tag, cluster_template,
                                           sc_exe_fp)

    # Execute the commands and build up the body of an email with the
    # summarized results as well as the output in log file attachments.
    email_body, attachments = _execute_commands_and_build_email(
            test_suites, setup_cmds, test_suites_cmds, teardown_cmds,
            setup_timeout, test_suites_timeout, teardown_timeout, cluster_tag)

    # Send the email.
    # TODO: this should be configurable by the user.
    subject = "Test suite results [automated testing system]"
    _send_email(email_settings['smtp_server'], email_settings['smtp_port'],
                email_settings['sender'], email_settings['password'],
                recipients, subject, email_body, attachments)

def _parse_config_file(config_f):
    """Parses and validates a configuration file describing test suites.

    Returns a list of lists containing the test suite label as the first
    element and the command string needed to execute the test suite as the
    second element.

    Arguments:
        config_f - the input configuration file describing test suites
    """
    results = []
    used_test_suite_names = []
    for line in config_f:
        if not _can_ignore(line):
            fields = line.strip().split('\t')
            if len(fields) != 2:
                raise ValueError("Each line in the config file must contain "
                                 "exactly two fields separated by tabs.")
            if fields[0] in used_test_suite_names:
                raise ValueError("The test suite label '%s' has already been "
                                 "used. Each test suite label must be unique."
                                 % fields[0])
            results.append(fields)
            used_test_suite_names.append(fields[0])
    if len(results) == 0:
        raise ValueError("The config file must contain at least one test "
                         "suite to run.")
    return results

def _parse_email_list(email_list_f):
    """Parses and validates a file containing email addresses.
    
    Returns a list of email addresses.

    Arguments:
        email_list_f - the input file containing email addresses
    """
    recipients = [line.strip() for line in email_list_f \
                  if not _can_ignore(line)]
    if len(recipients) == 0:
        raise ValueError("There are no email addresses to send the test suite "
                         "results to.")
    for address in recipients:
        if '@' not in address:
            raise ValueError("The email address '%s' doesn't look like a "
                             "valid email address." % address)
    return recipients

def _parse_email_settings(email_settings_f):
    """Parses and validates a file containing email SMTP settings.

    Returns a dictionary with the key/value pairs 'smtp_server', 'smtp_port',
    'sender', and 'password' defined.

    Arguments:
        email_settings_f - the input file containing tab-separated email
            settings
    """
    required_fields = ['smtp_server', 'smtp_port', 'sender', 'password']
    settings = {}
    for line in email_settings_f:
        if not _can_ignore(line):
            try:
                setting, val = line.strip().split('\t')
            except:
                raise ValueError("The line '%s' in the email settings file "
                                 "must have exactly two fields separated by a "
                                 "tab." % line)
            if setting not in required_fields:
                raise ValueError("Unrecognized setting '%s' in email settings "
                                 "file. Valid settings are %r." % (setting,
                                 required_fields))
            settings[setting] = val
    if len(settings) != 4:
        raise ValueError("The email settings file does not contain one or "
                "more of the following required fields: %r" % required_fields)
    return settings

def _build_test_execution_commands(test_suites, sc_config_fp, user,
                                   cluster_tag, cluster_template=None,
                                   sc_exe_fp='starcluster'):
    """Builds up commands that need to be executed to run the test suites.

    These commands are starcluster commands to start/terminate a cluster,
    (setup/teardown commands, respectively) as well as execute commands over
    ssh to run the test suites (test suite commands).

    Returns a 3-element tuple containing the list of setup command strings,
    the list of test suite command strings, and the list of teardown command
    strings.

    Arguments:
        test_suites - the output of _parse_config_file()
        sc_config_fp - same as for run_test_suites()
        user - same as for run_test_suites()
        cluster_tag - same as for run_test_suites()
        cluster_template - same as for run_test_suites()
        sc_exe_fp - same as for run_test_suites()
    """
    setup_cmds, test_suite_cmds, teardown_cmds = [], [], []

    sc_start_cmd = "%s -c %s start " % (sc_exe_fp, sc_config_fp)
    if cluster_template is not None:
        sc_start_cmd += "-c %s " % cluster_template
    sc_start_cmd += "%s" % cluster_tag
    setup_cmds.append(sc_start_cmd)

    for test_suite_name, test_suite_exec in test_suites:
        # To have the next command work without getting prompted to accept the
        # new host, the user must have 'StrictHostKeyChecking no' in their SSH
        # config (on the local machine). TODO: try to get starcluster devs to
        # add this feature to sshmaster.
        test_suite_cmds.append("%s -c %s sshmaster -u %s %s '%s'" %
                (sc_exe_fp, sc_config_fp, user, cluster_tag, test_suite_exec))

    # The second -c tells starcluster not to prompt us for termination
    # confirmation.
    teardown_cmds.append("%s -c %s terminate -c %s" % (sc_exe_fp, sc_config_fp,
                                                       cluster_tag))
    return setup_cmds, test_suite_cmds, teardown_cmds

def _execute_commands_and_build_email(test_suites, setup_cmds,
                                      test_suites_cmds, teardown_cmds,
                                      setup_timeout, test_suites_timeout,
                                      teardown_timeout, cluster_tag):
    """Executes the test suite commands and builds the body of an email.

    Returns the body of an email containing the summarized results and any
    error message or issues that should be brought to the recipient's
    attention, and a list of attachments, which are the log files from running
    the commands.

    Arguments:
        test_suites - the output of _parse_config_file()
        setup_cmds - the output of _build_test_execution_commands()
        test_suites_cmds - the output of _build_test_execution_commands()
        teardown_cmds - the output of _build_test_execution_commands()
        setup_timeout - same as for run_test_suites()
        test_suites_timeout - same as for run_test_suites()
        teardown_timeout - same as for run_test_suites()
        cluster_tag - same as for run_test_suites()
    """
    email_body = ""
    attachments = []

    # Create a unique temporary file to hold the results of all commands.
    log_f = TemporaryFile(prefix='automated_testing_log', suffix='.txt')
    attachments.append(('automated_testing_log.txt', log_f))

    # Build up the body of the email as we execute the commands. First, execute
    # the setup commands.
    cmd_executor = CommandExecutor(setup_cmds, log_f,
                                   stop_on_first_failure=True)
    setup_cmds_succeeded = cmd_executor(setup_timeout)[0]

    if setup_cmds_succeeded is None:
        email_body += ("The maximum allowable cluster setup time of %s "
                       "minute(s) was exceeded.\n\n" % str(setup_timeout))
    elif not setup_cmds_succeeded:
        email_body += ("There were problems in starting the remote cluster "
                       "while preparing to execute the test suite(s). Please "
                       "check the attached log for more details.\n\n")
    else:
        # Execute each test suite command, keeping track of stdout and stderr
        # in a temporary file. These will be used as attachments when the
        # email is sent. Since the temporary files will have randomly-generated
        # names, we'll also specify what we want the file to be called when it
        # is attached to the email (we don't have to worry about having unique
        # filenames at that point).
        cmd_executor.cmds = test_suites_cmds
        cmd_executor.stop_on_first_failure = False
        cmd_executor.log_individual_cmds = True
        test_suites_cmds_succeeded, test_suites_cmds_status = \
                cmd_executor(test_suites_timeout)

        # It is okay if there are fewer test suites that got executed than
        # there were input test suites (which is possible if we encounter a
        # timeout). Just report the ones that finished.
        label_to_ret_val = []
        for (label, cmd), (test_suite_log_f, ret_val) in \
                zip(test_suites, test_suites_cmds_status):
            label_to_ret_val.append((label, ret_val))
            attachments.append(('%s_results.txt' % label, test_suite_log_f))

        # Build a summary of the test suites that passed and those that didn't.
        email_body += _build_email_summary(label_to_ret_val)

        if test_suites_cmds_succeeded is None:
            timeout_test_suite = \
                    test_suites[len(test_suites_cmds_status) - 1][0]
            untested_suites = [label for label, cmd in
                               test_suites[len(test_suites_cmds_status):]]
            email_body += ("The maximum allowable time of %s minute(s) for "
                           "all test suites to run was exceeded. The timeout "
                           "occurred while running the %s test suite." %
                           (str(test_suites_timeout), timeout_test_suite))
            if untested_suites:
                email_body += (" The following test suites were not tested: "
                               "%s\n\n" % ', '.join(untested_suites))

    # Lastly, execute the teardown commands.
    cluster_termination_msg = ("IMPORTANT: You should check that the cluster "
                               "labelled with the tag '%s' was properly "
                               "terminated. If not, you should manually "
                               "terminate it.\n\n" % cluster_tag)

    cmd_executor.cmds = teardown_cmds
    cmd_executor.stop_on_first_failure = False
    cmd_executor.log_individual_cmds = False
    teardown_cmds_succeeded = cmd_executor(teardown_timeout)[0]

    if teardown_cmds_succeeded is None:
        email_body += ("The maximum allowable cluster termination time of "
                       "%s minute(s) was exceeded.\n\n%s" %
                       (str(teardown_timeout), cluster_termination_msg))
    elif not teardown_cmds_succeeded:
        email_body += ("There were problems in terminating the remote "
                       "cluster. Please check the attached log for more "
                       "details.\n\n%s" % cluster_termination_msg)

    # Set our file position to the beginning for all attachments since we are
    # in read/write mode and we need to read from the beginning again. Closing
    # the file will delete it.
    for attachment in attachments:
        attachment[1].seek(0, 0)

    return email_body, attachments

def _build_email_summary(test_suites_status):
    """Builds up a string suitable for the body of an email message.

    Returns a string containing a summary of the testing results for each of
    the test suites. The summary will list the test suite name and whether it
    passed or not (which is dependent on the status of the return code of the
    test suite).

    Arguments:
        test_suites_status - a list of 2-element tuples, where the first
        element is the test suite label and the second element is the return
        value of the command that was run for the test suite. A non-zero return
        value indicates that something went wrong or the test suite didn't pass
    """
    summary = ''
    for test_suite_label, ret_val in test_suites_status:
        summary += test_suite_label + ': '
        summary += 'Pass\n' if ret_val == 0 else 'Fail\n'
    if summary != '':
        summary += '\n'
    return summary

def _send_email(host, port, sender, password, recipients, subject, body,
               attachments=None):
    """Sends an email (optionally with attachments).

    This function does not return anything. It is not unit tested because it
    sends an actual email.

    This code is largely based on the code found here:
    http://www.blog.pythonlibrary.org/2010/05/14/how-to-send-email-with-python/
    http://segfault.in/2010/12/sending-gmail-from-python/

    Arguments:
        host - the STMP server to send the email with
        port - the port number of the SMTP server to connect to
        sender - the sender email address (i.e. who this message is from). This
            will be used as the username when logging into the SMTP server
        password - the password to log into the SMTP server with
        recipients - a list of email addresses to send the email to
        subject - the subject of the email
        body - the body of the email
        attachments - a list of 2-element tuples, where the first element is
            the filename that will be used for the email attachment (as the
            recipient will see it), and the second element is the file to be
            attached
    """
    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = ', '.join(recipients)
    msg['Subject'] = subject
    msg['Date'] = formatdate(localtime=True)
 
    if attachments is not None:
        for attachment_name, attachment_f in attachments:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(attachment_f.read())
            encode_base64(part)
            part.add_header('Content-Disposition',
                            'attachment; filename="%s"' % attachment_name)
            msg.attach(part)
    part = MIMEText('text', 'plain')
    part.set_payload(body)
    msg.attach(part)
 
    server = SMTP(host, port)
    server.ehlo()
    server.starttls()
    server.ehlo
    server.login(sender, password)
    server.sendmail(sender, recipients, msg.as_string())
    server.quit()

def _can_ignore(line):
    """Returns True if the line can be ignored (comment or blank line)."""
    return False if line.strip() != '' and not line.strip().startswith('#') \
           else True

class CommandExecutor(object):
    """Class to run commands in a separate thread.

    Provides support for timeouts (e.g. useful for commands that may hang
    indefinitely) and for capturing stdout, stderr, and return value of each
    command. Output is logged to a file (or optionally to separate files for
    each command).

    This class is the single place in Clout that is not platform-independent
    (it won't be able to terminate timed-out processes on Windows). The fix is
    to not use shell=True in our call to Popen, but this would require changing
    the way we support test suite config files and this (large) change will
    have to wait.

    Some of the code in this class is based on ideas/code from QIIME's
    util.qiime_system_call function and the following posts:
        http://stackoverflow.com/a/4825933
        http://stackoverflow.com/a/4791612
    """

    def __init__(self, cmds, log_f, stop_on_first_failure=False,
                 log_individual_cmds=False):
        """Initializes a new object to execute multiple commands.

        Arguments:
            cmds - list of commands to run (strings)
            log_f - the file to write command output to
            stop_on_first_failure - if True, will stop running all other
                commands once a command has a nonzero exit code
            log_individual_cmds - if True, will create a TemporaryFile for each
                command that is run and log the output separately (as well as
                to log_f). Will also keep track of the return values for each
                command
        """
        self.cmds = cmds
        self.log_f = log_f
        self.stop_on_first_failure = stop_on_first_failure
        self.log_individual_cmds = log_individual_cmds

    def __call__(self, timeout):
        """Executes the commands within the given timeout, logging output.

        If this method is called multiple times using the same members, the
        output of the commands will be appended to the existing log_f.

        Returns a 2-element tuple where the first element is a logical, where
        True indicates all commands succeeded, False indicates at least one
        command failed, and None indicates a timeout occurred.

        The second element of the tuple will be an empty list if
        log_individual_cmds is False, otherwise will be filled with 2-element
        tuples containing the individual TemporaryFile log file for each
        command, and the command's return code.

        Arguments:
            timeout - the number of minutes to allow all of the commands (i.e.
                self.cmds)to run collectively before aborting and returning the
                current results. Must be a float, to allow for fractions of a
                minute
        """
        self._cmds_succeeded = True
        self._individual_cmds_status = []

        # We must create locks for the next two variables because they are
        # read/written in the main thread and worker thread. They allow
        # the threads to communicate when a timeout has occurred, and the
        # hung process that needs to be terminated.
        self._running_process = None
        self._running_process_lock = Lock()

        self._timeout_occurred = False
        self._timeout_occurred_lock = Lock()

        # Run the commands in a worker thread. Regain control after the
        # specified timeout.
        cmd_runner_thread = Thread(target=self._run_commands)
        cmd_runner_thread.start()
        cmd_runner_thread.join(float(timeout) * 60.0)

        if cmd_runner_thread.is_alive():
            # Timeout occurred, so terminate the current process and have the
            # worker thread exit gracefully.
            with self._timeout_occurred_lock:
                self._timeout_occurred = True

            with self._running_process_lock:
                if self._running_process is not None:
                    # We must kill the process group because the process was
                    # launched with a shell. This code won't work on Windows.
                    killpg(self._running_process.pid, SIGTERM)
            cmd_runner_thread.join()

        return self._cmds_succeeded, self._individual_cmds_status

    def _run_commands(self):
        """Code to be run in worker thread; actually executes the commands."""
        for cmd in self.cmds:
            # Check that there hasn't been a timeout before running the (next)
            # command.
            with self._timeout_occurred_lock:
                if self._timeout_occurred:
                    self._cmds_succeeded = None
                    break
                else:
                    with self._running_process_lock:
                        # setsid makes the spawned shell the process group
                        # leader, so that we can kill it and its children from
                        # the main thread.
                        proc = Popen(cmd, shell=True, universal_newlines=True,
                                     stdout=PIPE, stderr=PIPE,
                                     preexec_fn=setsid)
                        self._running_process = proc

            # Communicate pulls all stdout/stderr from the PIPEs to avoid
            # blocking-- don't remove this line! This call blocks until the
            # command finishes (or is terminated by the main thread).
            stdout, stderr = proc.communicate()
            ret_val = proc.returncode

            with self._running_process_lock:
                self._running_process = None

            cmd_str = 'Command:\n\n%s\n\n' % cmd
            stdout_str = 'Stdout:\n\n%s\n' % stdout
            stderr_str = 'Stderr:\n\n%s\n' % stderr
            self.log_f.write(cmd_str + stdout_str + stderr_str)

            if self.log_individual_cmds:
                individual_cmd_log_f = TemporaryFile(
                        prefix='automated_testing_log', suffix='.txt')
                individual_cmd_log_f.write(cmd_str + stdout_str + stderr_str)
                self._individual_cmds_status.append(
                        (individual_cmd_log_f, ret_val))

            with self._timeout_occurred_lock:
                if ret_val != 0:
                    self._cmds_succeeded = False
                if self._timeout_occurred:
                    self._cmds_succeeded = None

                if self._timeout_occurred or \
                   (not self._cmds_succeeded and self.stop_on_first_failure):
                    break
