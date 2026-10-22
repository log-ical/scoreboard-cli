try:
    from .app import main
    main()
except KeyboardInterrupt:   # Ctrl-C while it starts up
    pass
