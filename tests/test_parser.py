import pytest 
from tempfile import TemporaryFile
from RCAEval.logparser import EventTemplate



@pytest.mark.parametrize("pattern, no_matches, matches", [
    (
        "received ad request (context_words=[<*>])",
        ["abc", "received ad", "ad request (context_words=[])"],
        [
            "received ad request (context_words=[clothing])",
            "received ad request (context_words=[clothing, shoes])",
            "received ad request (context_words=[123])"
        ]
    ),
    (
        "SEVERE: Exception while executing runnable <*>",
        ["SEVERE: Exception while executing runnable", "abc", ""],
        [
            "SEVERE: Exception while executing runnable io.grpc.internal.ServerImpl$JumpToApplicationThreadServerStreamListener$1HalfClosed@7d71091e",
            "SEVERE: Exception while executing runnable io.grpc.internal.ServerImpl$JumpToApplicationThreadServerStreamListener$1HalfClosed@293e648c"
        ]
    ) 
])
def test_load_template(pattern, no_matches, matches):
    template = EventTemplate(pattern)
    
    # Test strings that should NOT match
    for no_match in no_matches:
        assert not template.match(no_match)
    
    # Test strings that should match
    for match in matches:
        assert template.match(match)


def test_load_templates():
    temfile = TemporaryFile(mode='w+')
    temfile.write(
        "# This is a comment\n"
        "received ad request (context_words=[<*>])\n"
        "SEVERE: Exception while executing runnable <*>"
    )
    temfile.seek(0)
    templates = EventTemplate.load_templates(temfile.name)
    assert len(templates) == 2
    assert isinstance(templates[0], EventTemplate)
    assert isinstance(templates[1], EventTemplate)
    assert templates[0].template == "received ad request (context_words=[<*>])"
    assert templates[1].template == "SEVERE: Exception while executing runnable <*>"


def test_matching():
    template_file = TemporaryFile(mode='w+')
    template_file.write(
        "# This is a comment\n"
        "received ad request (context_words=[<*>])\n"
        "SEVERE: Exception while executing runnable <*>"
    )
    template_file.seek(0)

    log_file = TemporaryFile(mode='w+')
    log_file.write(
        "received ad request (context_words=[clothing])\n"
        "SEVERE: Exception while executing runnable io.grpc.internal.ServerImpl$JumpToApplicationThreadServerStreamListener$1HalfClosed@7d71091e\n"
        "Match no thing\n"
        "received ad request (context_words=[])\n"
    )
    log_file.seek(0)

    df = EventTemplate.matchfile(
        template_file=template_file.name,
        log_file=log_file.name,
    )

    # df should have two columns ('log', and 'event type'), each rows = each log with the correspndin template
    assert df.shape[0] == 4
    assert df.shape[1] == 2
    assert df.columns.tolist() == ['log', 'event type']
    assert df.iloc[0]['log'] == "received ad request (context_words=[clothing])"
    assert df.iloc[0]['event type'] == "received ad request (context_words=[<*>])"




