NAME    := gortex-dashboard
VERSION ?= $(shell git describe --tags --abbrev=0 2>/dev/null || echo v0.0.0)
PLUGIN  := plugins/gortex-dashboard
SKILL   := $(PLUGIN)/skills/dashboard
DIST    := dist

.PHONY: validate run package release clean

validate:
	claude plugin validate $(PLUGIN)
	claude plugin validate .

run:
	python3 $(SKILL)/scripts/gortex-dashboard.py --serve

package:
	rm -rf $(DIST)
	mkdir -p $(DIST)
	tar czf $(DIST)/$(NAME)-plugin.tar.gz -C plugins $(NAME)
	tar czf $(DIST)/$(NAME)-skill.tar.gz -C $(PLUGIN)/skills dashboard
	( cd $(DIST) && sha256sum *.tar.gz > checksums.txt )
	@ls -l $(DIST)

release: package
	git tag -f $(VERSION)
	git push origin $(VERSION)
	gh release create $(VERSION) $(DIST)/$(NAME)-plugin.tar.gz $(DIST)/$(NAME)-skill.tar.gz \
		$(DIST)/checksums.txt --title "$(NAME) $(VERSION)" --generate-notes

clean:
	rm -rf $(DIST)
