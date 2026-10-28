test:
	/usr/bin/python3 -m unittest discover -s tests -v
	node tests/test_model.js
